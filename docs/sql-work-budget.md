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
and stays active through cursor iteration. It runs every `min(1000, max_vm_steps)` VM
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
does not impose a process-wide memory ceiling. SQLite native limits additionally
cap individual values/encoded records at `max(65536, max_result_bytes)` bytes,
SQL text at 65536 bytes, and LIKE/GLOB patterns at 1024 bytes. These limits are
installed before user statement preparation and binding; model arguments cannot
raise them. Existing large database records can also be rejected by this policy.
They do not bound total allocations across values, sort buffers, Python inputs,
or SQLite internals. Oversized bound values/concatenations raise a policy error.
SQLite printf instead returns SQL NULL when its native length limit is exceeded;
this is preserved as NULL, not represented as a successful formatted string.
Ordinary bounded printf formatting remains supported. No user-defined functions or extensions
are installed.

## Authorization boundary

Each call creates a private connection. Before preparing user SQL it installs
`set_authorizer`, allowing only SELECT, recursive CTE execution, reads of configured
ordinary tables in `main`, and an explicit list of built-in analytics functions.
Everything else is denied, including mutations, transaction control, ATTACH,
PRAGMA, schema introspection, unknown functions, views and virtual/shadow tables.
Named parameters remain bound by SQLite. SQLite parses all identifier quoting,
comma joins, nesting and CTEs; regex matching is not a security boundary.

SQLite reports `database=None` for columnless reads such as `count(*)`. These are
accepted only for configured ordinary tables on the private connection, which has
only `main` and an empty `temp` schema; ATTACH and creation are forbidden. Do not
replace this with a shared connection or register user functions/extensions. CTEs
may shadow names, but every actual base-table read is independently authorized.
At least one authorized base-table read must be compiled; SQLite may optimize
unused CTEs away. The policy is about actual SQLite access, not SQL text mentions.

Authorization remains installed through fetching and is discarded by closing the
connection on every exit path. No connection/statement cache is shared across
calls. Authorization failures return a stable error without partial results,
query text, parameters or SQLite exception details. A union with a permitted first
arm and forbidden second arm is denied before any response is constructed.

The database file/schema and Python process/configuration must be trusted. This
is table-level access control, not row/tenant/column isolation or a general SQL
security proof. It does not protect against SQLite implementation vulnerabilities,
malicious database files, or a compromised host. SQLite 3.37+ is needed for
`PRAGMA table_list`; older engines cannot establish the allowed table set and
fail closed. Cooperative budgets remain subject to the limitations above.

## Validation and next step

`python -m pytest tests/test_sql_budget.py -q` exercises real SQLite: a one-row
Cartesian aggregate, interruption while fetching, repeated recovery, server-owned
budget enforcement, invalid configuration, response compatibility, error
redaction, and connection cleanup on every exit path. These are correctness tests,
not a claim of measured throughput improvement.

Next, execute queries in a supervised worker with a process-level address-space
limit and authenticated execution receipt, retaining the same response-byte and
query-fingerprint contract.

`python -m pytest tests/test_sql_authorization.py -q` covers synthetic unauthorized
reads, quoted/schema-qualified names, comma joins, nested queries, CTEs, writes,
functions, views, virtual tables, concurrent reuse, and no-partial-result cleanup.
These fixture counts are not production security or performance measurements.

`python -m pytest tests/test_sql_native_limits.py -q` checks native limit installation,
a bounded 128 KiB printf request (NULL), ordinary formatting with aggregation,
large bindings and intermediate concatenation, SQL text and LIKE pattern limits.
No genuinely memory-exhausting query or process-level memory benchmark is used.
