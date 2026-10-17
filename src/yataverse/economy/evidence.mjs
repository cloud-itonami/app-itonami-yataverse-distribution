import { instantiateKotoba } from "../../../dist/service-evidence-core.mjs";
import {
  admitReceipt as admitWithCore,
  emptyLedger,
  verifyReceipt as verifyWithCore,
} from "./evidence-host.mjs";

const core = instantiateKotoba({});

export { emptyLedger };

export function verifyReceipt(receipt, verifiers) {
  return verifyWithCore(core, receipt, verifiers);
}

export function admitReceipt(ledger, receipt, verifiers) {
  return admitWithCore(core, ledger, receipt, verifiers);
}
