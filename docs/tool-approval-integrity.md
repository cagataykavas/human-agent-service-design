# Tool approval integrity

Human approval is an authorization for one exact write request, not a reusable signal that a
tool is generally safe. The service desk therefore binds approval to a canonical SHA-256 digest
of the job, server-owned risk, tool name, requesting worker and complete JSON arguments.

Every write request must also carry the target `issue_id` and its positive `expected_version`.
The target must be the issue owned by the durable agent job. Immediately before any side effect,
the worker must call `start_tool_call` with the reviewed digest while it owns the active job lease.
Admission atomically checks:

- the request still matches its stored digest;
- the human approval has not exceeded its bounded TTL;
- the target issue still has the reviewed optimistic version;
- the executing worker owns the current durable-job lease; and
- the call has not already been admitted or completed.

Only an admitted `executing` call can be completed, and only by the admitted execution owner.
This closes the gap between review and execution without pretending that the database can make an
external side effect atomic. A production adapter should use the call ID as an idempotency key at
the downstream service, persist the downstream receipt, and reconcile calls left in `executing`
after a worker crash.

Approval TTLs are restricted to 30–3,600 seconds and default to 15 minutes. Existing database rows
are migrated in place, but legacy calls without an original digest fail closed and must be
recreated. SHA-256 provides integrity evidence, not authenticity against an attacker who can alter
both rows and digests; deployments should protect the database and sign approval records or place
them in an append-only audit system when that threat is in scope.
