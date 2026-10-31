# Yataverse distribution agent instructions

Follow the shared workspace architecture, browser operation consent, Linear,
workspace placement, and preservation rules in the parent workspace AGENTS.md.

## CDCI: native qualification remains blocked (JUN-290 / JUN-228)

Removed `.github/workflows/amu-service-evidence.yml` on 2026-11-01. It fetched
`org-babashka-nbb` and passed its `cli.js` as `KBB_ENGINE` through
`scripts/build-service-evidence.mjs`. A `--jvm-free` flag did not make that build
Amu native. Do not restore this workflow, run that build as a gate, or silently
fall back to it. The README's CI build claim describes the removed legacy path.

There is no qualified selfhost native replacement gate established by this
change. Service-evidence compilation and its generated artifacts are no longer
covered by CI here; keep their native qualification blocked. Local POSIX
operations checks for HTTP identity, timeouts, redirects, and router ownership
are diagnostics only, and do not qualify the app, scheduler, or native runtime.

The shared owner rule allows a developer's one-time cljk/JVM/nbb reference
oracle only with dated tool/input/output evidence. Never put that oracle in
CI, gates, builds, packaging, product execution, or fallback paths.

Existing deployed Python/Node readers and stream-mount controllers are
migration inventory. This change grants no permission to stop them or claim
that their `.cljk` source establishes Amu native execution. Preserve them until
the shared compatibility, release, and separately approved cutover conditions
are met. DNS administration is distinct from the byte-delivery data path.
