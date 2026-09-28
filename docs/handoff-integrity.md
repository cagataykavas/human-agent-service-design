# Agent-to-human handoff integrity

Human review is only useful when the reviewer sees the exact case state and evidence that caused the
agent to escalate. A handoff generated for issue version 4 must not authorize an action after another
actor has changed the issue to version 5. A citation label alone must not be treated as evidence.

`service_desk.handoff` implements a fail-closed admission boundary for this gap. It binds a short-lived
handoff to the issue and optimistic version, agent job and state, capability, policy version, trace
digest, recommended action set, unresolved-question codes, and content-addressed evidence producers.
Admission rechecks all bindings against server-owned current state immediately before the reviewer is
shown an actionable packet.

## Security properties

- strict UTF-8 JSON with a 128 KiB limit, duplicate-field rejection, unknown-field rejection and no
  non-finite numbers;
- bounded actions, evidence references, questions, strings and artifact lifetime;
- canonical lowercase SHA-256 digests and deterministic privacy-preserving decision evidence;
- exact matching against a trusted evidence catalog, including both content digest and producer call;
- rejection of stale issue versions, changed jobs, traces, capabilities, policies or job states;
- stable finding codes without raw case, job, call, evidence or customer identifiers.

An accepted handoff is not authority to execute a tool. Existing role checks, human approval, active
job lease and optimistic workflow version remain mandatory at the write boundary. The handoff narrows
what a reviewer may consider; it does not replace those controls.

## Operational sequence

1. The agent runner persists its trace and admitted evidence.
2. The escalation producer creates a bounded handoff using the current issue version and policy ID.
3. The reviewer service loads current server-owned context and the trusted evidence catalog.
4. `audit_handoff` runs immediately before rendering actionable controls.
5. A rejected artifact is regenerated from current state; it is never patched by the client.
6. The final human decision separately enters the approval and workflow transition paths.

## Trust boundaries and limitations

SHA-256 provides content identity, not producer authenticity. Production deployments should sign the
artifact or store it in an append-only service controlled by the orchestration boundary. The trusted
evidence catalog must itself be built from admitted tool results; this module cannot establish factual
truth, source authorization or semantic sufficiency. Wall-clock expiry assumes bounded clock skew.
Concurrent state can still change after admission, so the eventual write must use the same optimistic
issue version and revalidate approval at execution time.

The next increment is to persist handoffs and reviewer decisions in the existing audit chain, expose a
read-only reviewer endpoint, and make the approved action consume the exact admitted handoff digest.
