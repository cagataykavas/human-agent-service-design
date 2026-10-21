# Server-owned SQL work budget

Returning at most 100 rows does not bound database work: `SELECT count(*)` over
large Cartesian joins returns one row after visiting many combinations. The
agent's read-only SQL tool now has four independent server-owned limits:

- `max_rows=100`: returned rows (one extra row is fetched to detect truncation).
- `max_vm_steps=100_000`: cooperative SQLite virtual-machine instruction budget.
- `max_result_bytes=65_536`: exact canonical JSON response ceiling, checked as
  rows are fetched and again on the final envelope.
- `max_execution_seconds=2.0`: monotonic execution deadline, also used to cap
  SQLite lock waiting.

Configure these in trusted Python code, not model tool arguments:

```python
tool = ReadOnlySQLTool(
    database_path,
    max_rows=50,
    max_vm_steps=200_000,
    max_result_bytes=32_768,
    max_execution_seconds=1.0,
)
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
budgets, so a rejected query cannot poison the next call. Connections close on
normal return, database errors, and budget interruption. Accepted responses include
a SHA-256 fingerprint over validated SQL and sorted parameter names. Parameter
values are deliberately excluded.

## Trust and resource boundary

The monotonic deadline is checked by SQLite's progress callback and around fetches;
the remaining budget also caps the busy timeout. It cannot preempt a single native
opcode or system call mid-execution, so it is not a hard real-time guarantee. The
result ceiling checks the exact serialized response after every accepted row, so
the remaining result set is never materialized after the ceiling is crossed. It
does not impose a process-wide memory ceiling, and a single SQLite value can be
allocated before the response check runs. No user-defined functions or extensions
are installed.

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

Next, execute queries in a supervised worker with a process-level address-space
limit and authenticated execution receipt, retaining the same response-byte and
query-fingerprint contract.
