# Service evidence on the Amu path

`src/yataverse/economy/evidence.kotoba` owns the receipt contract and state
transitions. It has no imports or host capabilities. It enforces the schema,
event-ID shape, bounded strings and decimal measurements, work-kind/category/
unit mapping, signer relationships, required verifier results, and replay
decision codes.

`scripts/build-service-evidence.mjs` compiles that module with the clean Amu
revision in `amu.lock` and the clean JavaScript engine revision in
`engine.lock`. The compiled `js-kotoba-v1` module must have no effects. Run it
with `AMU_ROOT` and `KBB_ENGINE_ROOT` set to those exact checkouts. The
`Amu service evidence` workflow performs this build for pull requests and main.

`src/yataverse/economy/evidence-host.mjs` is the representation and platform
adapter. `src/yataverse/economy/evidence.mjs` binds that adapter to the compiled
core and exposes `verifyReceipt`, `admitReceipt`, and `emptyLedger`. The host
projects parsed JSON into the scalar facts consumed by the core, calls injected
canonicalization, digest, signature, policy, and CID verifiers, and keeps the
explicitly in-memory replay maps. It does not decide whether any receipt causes
an economic action. The returned `economicActions` list is always empty.

The adapter's required `canonicalize` provider must produce the agreed JCS
representation. `digestValid` verifies that representation against `event-id`;
`signatureValid` checks the schema-separated message returned by the Amu core;
`policyValid` and `evidenceValid` are injected readers/verifiers. A missing
provider, malformed receipt, or false verifier result fails closed. Hosts must
use a durable, linearizable store before making replay protection a
multi-process guarantee; the provided `emptyLedger()` is in-memory only.

This module is not connected to the Yataverse follower, Itonami jobs, Kura,
Murakumo, YATA claims, or any payment path. It records evidence admission
semantics only; it does not qualify identity/key custody, a production
signature provider, persistent replay storage, or service deployment.
