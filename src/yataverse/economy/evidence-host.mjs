// Thin JavaScript host adapter for the Amu compiled evidence core.
// JSON shape projection and external crypto/storage APIs stay at this boundary;
// admission, bounds, work contracts, verification gating, and replay decisions
// are owned by the Kotoba module.

const RECEIPT_FIELDS = new Set([
  "schema", "event-id", "order-id", "provider-did", "work-kind",
  "service-category", "policy-version", "measure", "evidence-cids",
  "signatures", "nonce",
]);
const MEASURE_FIELDS = new Set(["kind", "value"]);
const SIGNATURES_FIELDS = new Set(["provider", "witnesses"]);
const SIGNER_FIELDS = new Set(["did", "signature"]);
const ECONOMIC_FIELDS = new Set([
  "token", "token-symbol", "amount", "currency", "chain", "epoch",
  "root", "payee", "recipient", "payout", "settlement", "conversion",
  "exchange-rate",
]);
const PRIVATE_FIELDS = new Set([
  "prompt", "customer-payload", "request-body", "personal-data",
]);

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && (Object.getPrototypeOf(value) === Object.prototype
      || Object.getPrototypeOf(value) === null);
}

function hasExactFields(value, expected) {
  return isRecord(value)
    && Object.keys(value).length === expected.size
    && Object.keys(value).every((key) => expected.has(key));
}

function forbiddenKeysAbsent(value, forbiddenKeys) {
  const seen = new WeakSet();
  let nodes = 0;
  function visit(item, depth) {
    if (depth > 8 || ++nodes > 4096) return false;
    if (Array.isArray(item)) return item.every((child) => visit(child, depth + 1));
    if (item === null || typeof item !== "object") return true;
    if (!isRecord(item)) return false;
    if (seen.has(item)) return false;
    seen.add(item);
    const entries = Object.entries(item);
    if (entries.length > 64) return false;
    return entries.every(([key, child]) =>
      !forbiddenKeys.has(key)
      && visit(child, depth + 1));
  }
  return visit(value, 0);
}

function normalizedString(value) {
  return typeof value === "string" ? value : "";
}

function projectReceipt(core, receipt) {
  const object = isRecord(receipt) ? receipt : Object.create(null);
  const measure = object.measure;
  const signatures = object.signatures;
  const provider = isRecord(signatures) ? signatures.provider : undefined;
  const witnesses = isRecord(signatures) ? signatures.witnesses : undefined;
  const cids = object["evidence-cids"];
  const providerDid = normalizedString(object["provider-did"]);
  const signerShape = isRecord(provider) && hasExactFields(provider, SIGNER_FIELDS);
  const witnessListShape = Array.isArray(witnesses) && witnesses.length <= 32;
  const witnessIds = witnessListShape
    ? witnesses.map((witness) => isRecord(witness) ? witness.did : undefined)
    : [];
  const witnessShapesValid = witnessListShape
    && witnesses.every((witness) => isRecord(witness)
      && hasExactFields(witness, SIGNER_FIELDS)
      && typeof witness.did === "string"
      && typeof witness.signature === "string"
      && core["did-valid?"](witness.did)
      && core["bounded-nonblank?"](witness.signature, 4096)
      && witness.did !== providerDid)
    && new Set(witnessIds).size === witnessIds.length;
  const evidenceShapeValid = Array.isArray(cids) && cids.length <= 64
    && cids.every((cid) => typeof cid === "string"
      && core["bounded-nonblank?"](cid, 512));

  const values = [
    normalizedString(object.schema),
    normalizedString(object["event-id"]),
    normalizedString(object["order-id"]),
    providerDid,
    normalizedString(object["work-kind"]),
    normalizedString(object["service-category"]),
    normalizedString(object["policy-version"]),
    normalizedString(isRecord(measure) ? measure.kind : undefined),
    normalizedString(isRecord(measure) ? measure.value : undefined),
    normalizedString(object.nonce),
    normalizedString(isRecord(provider) ? provider.did : undefined),
    normalizedString(isRecord(provider) ? provider.signature : undefined),
    witnessListShape ? witnesses.length : 0,
    evidenceShapeValid ? cids.length : 0,
    isRecord(receipt) && Object.keys(receipt).every((key) => RECEIPT_FIELDS.has(key)),
    hasExactFields(measure, MEASURE_FIELDS)
      && typeof measure.kind === "string" && typeof measure.value === "string",
    hasExactFields(signatures, SIGNATURES_FIELDS) && signerShape && witnessListShape,
    witnessShapesValid,
    evidenceShapeValid,
    forbiddenKeysAbsent(receipt, ECONOMIC_FIELDS),
    forbiddenKeysAbsent(receipt, PRIVATE_FIELDS),
  ];

  const valid = core["receipt-shape-valid?"](...values);
  return { valid, object, provider, witnesses, cids };
}

function requireVerifiers(verifiers) {
  for (const name of ["canonicalize", "digestValid", "signatureValid",
    "policyValid", "evidenceValid"]) {
    if (typeof verifiers?.[name] !== "function") {
      throw new TypeError(`service evidence verifier ${name} is required`);
    }
  }
}

function canonicalBody(receipt) {
  return Object.fromEntries(Object.entries(receipt)
    .filter(([key]) => key !== "event-id" && key !== "signatures"));
}

async function checkReceipt(core, receipt, verifiers) {
  requireVerifiers(verifiers);
  const projected = projectReceipt(core, receipt);
  if (!projected.valid) throw new TypeError("service evidence has invalid shape");

  const body = canonicalBody(projected.object);
  const canonical = await verifiers.canonicalize(body);
  if (typeof canonical !== "string" || canonical.length > 100_000) {
    throw new TypeError("canonical service evidence body is invalid or too large");
  }
  const digestValid = await verifiers.digestValid(canonical, projected.object["event-id"]);
  const message = core["signing-message"](projected.object["event-id"]);
  let providerSignatureValid = false;
  let witnessesSignaturesValid = false;
  if (digestValid === true) {
    providerSignatureValid = await verifiers.signatureValid(message, projected.provider) === true;
    witnessesSignaturesValid = providerSignatureValid
      && (await Promise.all(projected.witnesses.map((signer) =>
        verifiers.signatureValid(message, signer)))).every((result) => result === true);
  }
  let policyValid = false;
  let evidenceValid = false;
  if (digestValid === true && providerSignatureValid && witnessesSignaturesValid) {
    [policyValid, evidenceValid] = await Promise.all([
      verifiers.policyValid(projected.object),
      verifiers.evidenceValid(projected.object),
    ]);
    policyValid = policyValid === true;
    evidenceValid = evidenceValid === true;
  }
  if (!core["verification-complete?"](digestValid === true, providerSignatureValid,
    witnessesSignaturesValid, policyValid, evidenceValid)) {
    throw new TypeError("service evidence verification failed");
  }
  return { ...projected, body, canonical };
}

export function emptyLedger() {
  return { events: new Map(), nonces: new Map() };
}

export async function verifyReceipt(core, receipt, verifiers) {
  await checkReceipt(core, receipt, verifiers);
  return receipt;
}

export async function admitReceipt(core, ledger, receipt, verifiers) {
  if (!(ledger?.events instanceof Map) || !(ledger?.nonces instanceof Map)) {
    throw new TypeError("service evidence ledger must contain event and nonce maps");
  }
  const checked = await checkReceipt(core, receipt, verifiers);
  const eventId = checked.object["event-id"];
  const orderId = checked.object["order-id"];
  const nonce = checked.object.nonce;
  const nonceKey = `${orderId.length}:${orderId}${nonce.length}:${nonce}`;
  const priorBody = ledger.events.get(eventId);
  const decision = Number(core["admission-code"](ledger.events.has(eventId),
    priorBody === checked.canonical, ledger.nonces.has(nonceKey)));

  if (decision === 1) {
    return { ledger, status: "duplicate", eventId, economicActions: [] };
  }
  if (decision === -1) throw new TypeError("service evidence event-id collision");
  if (decision === -2) {
    throw new TypeError("service evidence nonce already used for this order");
  }
  if (decision !== 0) throw new TypeError("service evidence core returned an invalid replay decision");
  ledger.events.set(eventId, checked.canonical);
  ledger.nonces.set(nonceKey, eventId);
  return { ledger, status: "accepted", eventId, economicActions: [] };
}
