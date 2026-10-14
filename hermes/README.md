# Yataverse resident research bot

`yataverse-storage-research` owns the lossless/distributed-storage research for this repository.
Its Itonami/Hermes profile is [`profiles/yataverse-storage-research/`](profiles/yataverse-storage-research/).
The profile ships no lake measurements, credentials, private endpoints, keys or host run state.
Private handoff, provider binding, workspace placement and execution receipts stay on the host.

The native cron definition is a bounded six-hour research checkpoint, not a scheduled full-lake load test.
Materializing this directory does not itself prove Itonami Bot creation, grants, a successful turn,
a running scheduler or cloud-equivalent durability. Host activation must verify those separately.
Source uses the owning-repository profile layout; preserve host-local provider credentials and state.

The shipped schedule starts disabled. Activation requires a scoped private-data/provider decision,
a valid Itonami registration path, and a verified first checkpoint. The local resident app also
supports credential-free Hermes migration through its existing agent-session import API;
ordinary Bot creation still requires a human session. The host-private
handoff may contain internal evidence; source-profile merge does not authorize sending it to a model.
A missing multiplex gateway is not permission to start unrelated bots.

## Profile-only retry dispatcher

`profiles/yataverse-storage-research/scripts/dispatch.cljk` makes one bounded research checkpoint without starting the shared
Hermes gateway. An operator installs a profile-specific timer only after explicit private-input/provider
consent. The shipped cron remains disabled to avoid duplicate execution. A failed or timed-out attempt
is recorded as waiting-provider-or-agent, never completed; research qualification still needs a real
checkpoint. The dispatcher checks four approved evidence hashes, HANDOFF/manifest/prompt hashes,
provider/model metadata, a dedicated worktree, an exclusive per-profile lock and a 480-second
outer process-group timeout. A local STOP_DISPATCH marker stops new attempts.

The generic interactive CLI adds roughly 25KB of system/tool definitions before evidence and
may enter source-install completion under an isolated home. The checkpoint instead uses
`scripts/review.cljk`: it reads and hashes the four approved receipts locally, projects an
allowlisted summary (maximum 10KB request), performs nine small synthetic shard-loss restores,
and asks the approved Murakumo/mishima endpoint to select validated findings and the next
synthetic entropy pattern using a closed JSON schema. The model receives
no executable tools. Redirects, model drift, truncated replies, free-text claims and unsupported experiments are refused;
numerical results are rendered from measured facts. Model/API charges remain an unpriced TCO input.
This is a recurring bounded research workflow, not an unrestricted coding agent or a new whole-lake
load test. Current-cloud equivalence and actual TCO remain unqualified. Each successful checkpoint
retains a private JSON receipt and updates RESEARCH_REPORT.md, status.json and EXPERIMENT_PLAN.md.

The private operator policy and launch service stay on the host. These consistency checks are not
a new authentication authority or a filesystem sandbox. Test-auth sessions are never installed into
the real Itonami account. The checkpoint uses the existing provider credential only for HTTP authentication.
