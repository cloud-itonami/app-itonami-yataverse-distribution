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
a valid Itonami human session for Bot registration, and a verified first turn. The host-private
handoff may contain internal evidence; source-profile merge does not authorize sending it to a model.
A missing multiplex gateway is not permission to start unrelated bots.

## Profile-only retry dispatcher

`profiles/yataverse-storage-research/scripts/dispatch.cljk` can make one bounded native-agent attempt without starting the shared
Hermes gateway. An operator installs a profile-specific timer only after explicit private-input/provider
consent. The shipped cron remains disabled to avoid duplicate execution. A failed or timed-out attempt
is recorded as waiting-provider-or-agent, never completed; research qualification still needs a real
first turn. The dispatcher checks four approved evidence hashes, HANDOFF/manifest/prompt hashes,
provider/model metadata, a dedicated worktree, and an exclusive per-profile lock. It applies low
priority and a 240-second outer process-group timeout. A local STOP_DISPATCH marker stops new attempts.

The private operator policy and launch service stay on the host. These consistency checks are not
a new authentication authority or a filesystem sandbox. Test-auth sessions are never installed into
the real Itonami account. Credentials remain handled by the existing native wrapper.
