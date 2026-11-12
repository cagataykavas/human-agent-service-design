# Durable agent-run audit

`service-desk-run-audit` is a read-only gate over the service desk's persisted `agent_jobs` and
`tool_calls`. It revalidates evidence inside one SQLite read transaction instead of trusting the
application code that originally wrote each row.

The audit checks:

- job-state, attempt, lease, timestamp, output, and error coherence;
- strict, bounded JSON without duplicate fields or non-finite numbers;
- capability-specific contracts for triage, summaries, recommendations, and retrieval;
- exact retrieval lineage from the model query to one successful read-only `knowledge_search`
  call and from its result to the evidence stored in the final agent output;
- bounded job and tool-call counts before rows are loaded into memory.

Run it against a stopped or live WAL-mode service desk:

```bash
service-desk-run-audit --database service-desk.db --min-jobs 20 > agent-run-audit.json
```

The command exits `0` for a passing audit, `2` for evidence findings, and `3` when the database or
audit request cannot be processed safely. Reports contain aggregate state/capability/finding
counts, truncated SHA-256 job references, a commitment over the exact audited history, and a
self-digest. They do not include job IDs, issue IDs, prompts, model outputs, evidence text, tool
arguments/results, or error messages.

## Trust boundary and limitations

- The gate proves structural and causal consistency of stored records, not that model output or
  retrieved documents are factually correct, current, or unbiased.
- SHA-256 commitments are tamper evidence only when a trusted report digest is retained outside
  the database's failure domain.
- SQLite supplies one transaction-consistent view, but an administrator can rewrite the database
  before the audit starts.
- Retrieval equality binds the stored output to the stored tool result. A compromised tool can
  still return false content consistently.
- The current capability contracts match the version-1 service desk. Schema versioning and
  migration rules should accompany future capability changes.

The next production step is to invoke this gate before promoting an agent/model configuration,
sign the report digest, and retain it with the release decision.
