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
