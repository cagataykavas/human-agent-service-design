"""Real SQLite regression cases; all records are synthetic."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from service_desk.errors import ToolPolicyViolation
from service_desk.tools import ReadOnlySQLTool, ToolContext

CONTEXT = ToolContext("test", "synthetic-authorization")
DENIED = [
    'SELECT h.value FROM issues i, "hidden" h',
    "SELECT h.value FROM issues i, hidden h",
    'SELECT (SELECT value FROM "hidden") FROM issues',
    'SELECT value FROM "hidden"',
    "SELECT value FROM [hidden]",
    "SELECT value FROM `hidden`",
    "SELECT value FROM main.hidden",
    'SELECT h.value FROM issues i JOIN "hidden" h ON 1=1',
    'WITH x AS (SELECT value FROM "hidden") SELECT * FROM x',
    'WITH issues AS (SELECT value FROM "hidden") SELECT * FROM issues',
    'SELECT value FROM issues UNION ALL SELECT value FROM "hidden"',
    'SELECT value FROM issues WHERE EXISTS (SELECT 1 FROM "hidden")',
    'SELECT value FROM issues ORDER BY (SELECT value FROM "hidden")',
    'SELECT value FROM issues LIMIT (SELECT length(value) FROM "hidden")',
    "SELECT sql FROM sqlite_schema",
    "SELECT * FROM leak_view",
    'SELECT value FROM issues, pragma_table_info("hidden")',
    "SELECT load_extension(:path) FROM issues",
    "SELECT randomblob(100) FROM issues",
    "SELECT sqlite_version() FROM issues",
    "DELETE FROM issues",
    "WITH x AS (SELECT value FROM issues) DELETE FROM issues",
    "UPDATE issues SET value=0",
    "INSERT INTO issues VALUES (9)",
    "CREATE TABLE forbidden (value)",
    "DROP TABLE issues",
    "ATTACH DATABASE ':memory:' AS extra",
    "DETACH DATABASE main",
    "PRAGMA table_info(hidden)",
    "PRAGMA query_only=OFF",
    "BEGIN",
    "SELECT value FROM issues; SELECT value FROM hidden",
    "SELECT 1",
    "WITH x AS (SELECT 1) SELECT * FROM x",
]
VALID = [
    ("SELECT value FROM issues WHERE value=:value", {"value": 1}),
    ('SELECT value FROM "issues"', {}),
    ("SELECT value FROM [issues]", {}),
    ("SELECT value FROM `issues`", {}),
    ("SELECT value FROM main.issues", {}),
    ('SELECT value FROM "main"."issues"', {}),
    ("SELECT value FROM ISSUES", {}),
    ("SELECT i.value FROM issues i, projects p", {}),
    ("SELECT i.value FROM issues i JOIN projects p ON i.value=p.value", {}),
    ("WITH x AS (SELECT value FROM issues) SELECT value FROM x", {}),
    ("WITH hidden AS (SELECT value FROM issues) SELECT value FROM hidden", {}),
    ("SELECT count(*) AS value FROM issues", {}),
    ("SELECT max(value) AS value FROM issues", {}),
    ("SELECT value FROM issues WHERE value IN (SELECT value FROM projects)", {}),
    ("SELECT value FROM issues UNION SELECT value FROM projects", {}),
    ("SELECT 'DELETE; FROM hidden' AS value FROM issues", {}),
    ("/* comment */ SELECT value FROM issues;", {}),
    (
        "WITH RECURSIVE x(value) AS (SELECT value FROM issues UNION ALL SELECT value+1 FROM x WHERE value<3) SELECT * FROM x",
        {},
    ),
]


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "synthetic.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE issues(value); INSERT INTO issues VALUES(1); "
            "CREATE TABLE projects(value); INSERT INTO projects VALUES(1); "
            "CREATE TABLE hidden(value); INSERT INTO hidden VALUES('synthetic-secret'); "
            "CREATE VIEW leak_view AS SELECT value FROM hidden;"
        )
    return str(path)


@pytest.mark.parametrize("sql", DENIED)
def test_denied_query_returns_no_data(database, sql):
    tool = ReadOnlySQLTool(database)
    with pytest.raises(ToolPolicyViolation) as error:
        tool.execute({"sql": sql, "parameters": {"path": "never-load"}}, CONTEXT)
    assert "synthetic-secret" not in str(error.value)
    assert tool.execute({"sql": "SELECT value FROM issues"}, CONTEXT)["rows"] == [{"value": 1}]


@pytest.mark.parametrize("sql,parameters", VALID)
def test_valid_query(database, sql, parameters):
    result = ReadOnlySQLTool(database).execute({"sql": sql, "parameters": parameters}, CONTEXT)
    assert result["row_count"] >= 1


def test_view_cannot_be_allowlisted_as_a_base_table(database):
    with pytest.raises(ToolPolicyViolation):
        ReadOnlySQLTool(database, allowed_tables=frozenset({"leak_view", "hidden"})).execute(
            {"sql": "SELECT * FROM leak_view"}, CONTEXT
        )


def test_policy_is_server_owned_and_case_normalized(database):
    tool = ReadOnlySQLTool(database, allowed_tables=frozenset({"ISSUES"}))
    assert tool.execute({"sql": "SELECT value FROM issues"}, CONTEXT)["row_count"] == 1
    with pytest.raises(ToolPolicyViolation):
        tool.execute({"sql": DENIED[0], "allowed_tables": ["hidden", "issues"]}, CONTEXT)


def test_concurrent_allow_and_deny_reuse(database):
    tool = ReadOnlySQLTool(database)

    def run(denied):
        if denied:
            with pytest.raises(ToolPolicyViolation):
                tool.execute({"sql": DENIED[0]}, CONTEXT)
            return "denied"
        return tool.execute({"sql": "SELECT value FROM issues"}, CONTEXT)["row_count"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(run, [True, False] * 8)) == ["denied", 1] * 8


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM hidden",
        "SELECT 1 FROM hidden",
        "SELECT value FROM temp.issues",
        "SELECT value FROM extra.issues",
        "DELETE FROM issues RETURNING value",
        "WITH x AS (SELECT 1) UPDATE issues SET value=2 RETURNING value",
    ],
)
def test_schema_and_columnless_read_denials(database, sql):
    with pytest.raises(ToolPolicyViolation):
        ReadOnlySQLTool(database).execute({"sql": sql}, CONTEXT)


def test_denied_union_never_serializes_partial_result_and_connection_closes(database, monkeypatch):
    real_connect = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr("service_desk.tools.sqlite3.connect", connect)
    tool = ReadOnlySQLTool(database)

    def unexpected_result(*args, **kwargs):
        pytest.fail("A denied query must not produce even a partial response")

    monkeypatch.setattr(tool, "_result_payload", unexpected_result)
    with pytest.raises(ToolPolicyViolation):
        tool.execute(
            {"sql": 'SELECT value FROM issues UNION ALL SELECT value FROM "hidden"'}, CONTEXT
        )
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_unknown_function_is_denied_before_invocation(database, monkeypatch):
    real_connect = sqlite3.connect
    calls = []

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.create_function("side_effect", 0, lambda: calls.append(1) or 1)
        return conn

    monkeypatch.setattr("service_desk.tools.sqlite3.connect", connect)
    with pytest.raises(ToolPolicyViolation):
        ReadOnlySQLTool(database).execute({"sql": "SELECT side_effect() FROM issues"}, CONTEXT)
    assert calls == []


def test_virtual_table_is_not_an_ordinary_allowed_table(database):
    with sqlite3.connect(database) as db:
        db.execute("CREATE VIRTUAL TABLE search USING fts5(value)")
        db.execute("INSERT INTO search VALUES ('synthetic-secret')")
    with pytest.raises(ToolPolicyViolation):
        ReadOnlySQLTool(database, allowed_tables=frozenset({"search"})).execute(
            {"sql": "SELECT * FROM search"}, CONTEXT
        )
