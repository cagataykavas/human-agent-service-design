# Server-owned SQL work budget

Returning at most 100 rows does not bound database work: `SELECT count(*)` over
large Cartesian joins returns one row after visiting many combinations. The
agent's read-only SQL tool now has two independent, positive-integer limits:

- `max_rows=100`: returned rows (one extra row is fetched to detect truncation).
- `max_vm_steps=100_000`: cooperative SQLite virtual-machine instruction budget.

Configure these in trusted Python code, not model tool arguments:

```python
tool = ReadOnlySQLTool(database_path, max_rows=50, max_vm_steps=200_000)
```

The progress handler is installed before the untrusted query is prepared/executed
and stays active through `fetchmany`. It runs every `min(1000, max_vm_steps)` VM
instructions and interrupts at the first callback that reaches the budget.
For budgets not divisible by the interval, enforcement can occur up to 999 VM
instructions after the configured value. SQLite version/query plans affect work
counts; this is not a portable query-cost estimate or benchmark score.

An interrupted query raises the stable `ToolPolicyViolation` message
`query exceeded VM instruction budget`. It returns no partial rows and does not
echo the SQL or bound parameters. Each execution uses a fresh connection and
budget, so a rejected query cannot poison the next call. Connections close on
normal return, database errors, and budget interruption. Existing read-only mode,
query-only PRAGMA, table validation, bound parameters, and response fields remain.

## Trust and resource boundary

This does **not** guarantee a wall-clock deadline, memory ceiling, maximum result
bytes, or bounded database lock waits. A single opcode, disk operation, or native
extension/function can do substantial work between callbacks. SQLite's two-second
busy timeout concerns lock waits, not query execution time. No user-defined
functions or extensions are installed by this tool.

The existing regex SQL validator is not an AST parser or a complete table-access
security policy. Keep the read model non-sensitive and use least-privilege
database identities, an AST/authorizer policy, and process-level CPU/memory/time
limits before treating it as a production multi-tenant sandbox. Very small work
budgets can intentionally reject even simple queries.

## Validation and next step

`python -m pytest tests/test_sql_budget.py -q` exercises real SQLite: a one-row
Cartesian aggregate, interruption while fetching, repeated recovery, server-owned
budget enforcement, invalid configuration, response compatibility, error
redaction, and connection cleanup on every exit path. These are correctness tests,
not a claim of measured throughput improvement.

Next, execute agent queries in a supervised worker with a hard wall-clock deadline
and memory/result-byte ceilings; fingerprint queries for operational diagnostics
without storing raw customer parameters.
