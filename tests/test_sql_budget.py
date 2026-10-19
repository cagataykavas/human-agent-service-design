import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from service_desk.errors import ToolPolicyViolation
from service_desk.tools import ReadOnlySQLTool, ToolContext

CONTEXT = ToolContext("worker", "job-budget")
EXPENSIVE = "SELECT count(*) AS total FROM issues a CROSS JOIN issues b CROSS JOIN issues c"


@pytest.fixture
def database(tmp_path: Path) -> str:
    path = tmp_path / "read-model.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE issues (value INTEGER NOT NULL)")
        connection.executemany("INSERT INTO issues VALUES (?)", [(value,) for value in range(100)])
        connection.commit()
    finally:
        connection.close()
    return str(path)


def test_default_budget_stops_one_row_cartesian_aggregate(database):
    tool = ReadOnlySQLTool(database, max_rows=1)
    with pytest.raises(ToolPolicyViolation, match="VM instruction budget"):
        tool.execute({"sql": EXPENSIVE}, CONTEXT)


def test_valid_parameterized_query_preserves_contract(database):
    tool = ReadOnlySQLTool(database, max_rows=2, max_vm_steps=10_000)
    result = tool.execute(
        {"sql": "SELECT value FROM issues WHERE value >= :minimum", "parameters": {"minimum": 97}},
        CONTEXT,
    )
    assert result == {
        "columns": ["value"],
        "rows": [{"value": 97}, {"value": 98}],
        "row_count": 2,
        "truncated": True,
        "job_id": "job-budget",
    }


def test_budget_is_server_owned_not_a_tool_argument(database):
    tool = ReadOnlySQLTool(database, max_vm_steps=1000)
    with pytest.raises(ToolPolicyViolation, match="VM instruction budget"):
        tool.execute({"sql": EXPENSIVE, "max_vm_steps": 10**12}, CONTEXT)


def test_budget_resets_and_tool_recovers_after_interrupt(database):
    tool = ReadOnlySQLTool(database, max_vm_steps=1000)
    for _ in range(3):
        with pytest.raises(ToolPolicyViolation, match="VM instruction budget"):
            tool.execute({"sql": EXPENSIVE}, CONTEXT)
        result = tool.execute({"sql": "SELECT value FROM issues LIMIT 1"}, CONTEXT)
        assert result["rows"] == [{"value": 0}]


def test_concurrent_calls_have_independent_budgets(database):
    tool = ReadOnlySQLTool(database, max_vm_steps=1000)

    def run(expensive):
        if expensive:
            with pytest.raises(ToolPolicyViolation, match="VM instruction budget"):
                tool.execute({"sql": EXPENSIVE}, CONTEXT)
            return "rejected"
        return tool.execute({"sql": "SELECT value FROM issues LIMIT 1"}, CONTEXT)["row_count"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(run, [True, False] * 8)) == ["rejected", 1] * 8


def test_nonmultiple_budget_uses_documented_progress_granularity(database, monkeypatch):
    real_connect = sqlite3.connect
    intervals = []
    calls = []

    class TrackedConnection(sqlite3.Connection):
        def set_progress_handler(self, callback, interval):
            intervals.append(interval)

            def tracked_callback():
                calls.append(1)
                return callback()

            super().set_progress_handler(tracked_callback, interval)

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=TrackedConnection)

    monkeypatch.setattr("service_desk.tools.sqlite3.connect", connect)
    tool = ReadOnlySQLTool(database, max_vm_steps=1500)
    with pytest.raises(ToolPolicyViolation, match="VM instruction budget"):
        tool.execute({"sql": EXPENSIVE}, CONTEXT)
    assert intervals == [1000]
    assert len(calls) == 2


@pytest.mark.parametrize("setting", ["max_rows", "max_vm_steps"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "100", None])
def test_nonpositive_or_noninteger_budgets_rejected(database, setting, value):
    with pytest.raises(ValueError, match=setting):
        ReadOnlySQLTool(database, **{setting: value})


@pytest.mark.parametrize(
    "sql", [EXPENSIVE, "SELECT value FROM issues", "SELECT missing FROM issues"]
)
def test_connection_closed_after_execute_fetch_or_database_error(database, monkeypatch, sql):
    real_connect = sqlite3.connect
    connections = []

    class TrackedConnection(sqlite3.Connection):
        select_started = False

        def execute(self, statement, *args, **kwargs):
            cursor = super().execute(statement, *args, **kwargs)
            if statement.startswith("SELECT"):
                self.select_started = True
            return cursor

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr("service_desk.tools.sqlite3.connect", connect)
    tool = ReadOnlySQLTool(database, max_rows=1000, max_vm_steps=200)
    with pytest.raises(ToolPolicyViolation):
        tool.execute({"sql": sql}, CONTEXT)
    assert len(connections) == 1
    if sql == "SELECT value FROM issues":
        # execute() returned; the same budget must remain active through fetchmany().
        assert connections[0].select_started
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_connection_closed_on_success(database, monkeypatch):
    real_connect = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr("service_desk.tools.sqlite3.connect", connect)
    ReadOnlySQLTool(database).execute({"sql": "SELECT value FROM issues LIMIT 1"}, CONTEXT)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_smallest_budget_is_valid_and_fails_closed(database):
    tool = ReadOnlySQLTool(database, max_vm_steps=1)
    with pytest.raises(ToolPolicyViolation, match="VM instruction budget"):
        tool.execute({"sql": "SELECT value FROM issues LIMIT 1"}, CONTEXT)


def test_budget_error_does_not_echo_query_or_parameters(database):
    tool = ReadOnlySQLTool(database, max_vm_steps=1000)
    with pytest.raises(ToolPolicyViolation) as error:
        tool.execute(
            {
                "sql": EXPENSIVE + " WHERE :private = :private",
                "parameters": {"private": "customer"},
            },
            CONTEXT,
        )
    assert str(error.value) == "query exceeded VM instruction budget"
