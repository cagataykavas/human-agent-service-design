"""Bounded native SQLite probes, never memory-exhaustion experiments."""

import sqlite3

import pytest

from service_desk.errors import ToolPolicyViolation
from service_desk.tools import ReadOnlySQLTool, ToolContext

CONTEXT = ToolContext("test", "native-limits")


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "synthetic.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE issues(value); INSERT INTO issues VALUES(1);")
    return str(path)


def test_native_limits_installed_before_user_query(database, monkeypatch):
    real_connect = sqlite3.connect
    observed = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "SELECT value FROM issues":
                observed.append(
                    tuple(
                        self.getlimit(kind)
                        for kind in (
                            sqlite3.SQLITE_LIMIT_LENGTH,
                            sqlite3.SQLITE_LIMIT_SQL_LENGTH,
                            sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH,
                        )
                    )
                )
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(
        "service_desk.tools.sqlite3.connect",
        lambda *a, **k: real_connect(*a, **k, factory=Connection),
    )
    ReadOnlySQLTool(database).execute(
        {"sql": "SELECT value FROM issues", "max_sql_bytes": 10**9, "max_pattern_bytes": 10**9},
        CONTEXT,
    )
    assert observed == [(65536, 65536, 1024)]


def test_bounded_large_printf_returns_sql_null_not_an_oversized_value(database):
    # SQLite printf signals its native length limit as NULL, not SQLITE_TOOBIG.
    # Request only 128 KiB, never a genuinely memory-exhausting width.
    tool = ReadOnlySQLTool(database)
    result = tool.execute(
        {
            "sql": "SELECT printf('%0*d', :width, value) AS formatted FROM issues",
            "parameters": {"width": 128 * 1024},
        },
        CONTEXT,
    )
    assert result["rows"] == [{"formatted": None}]
    valid = tool.execute(
        {"sql": "SELECT printf('%04d', sum(value)) AS formatted FROM issues"}, CONTEXT
    )
    assert valid["rows"] == [{"formatted": "0001"}]


def test_native_value_limit_rejects_large_bound_value(database):
    with pytest.raises(ToolPolicyViolation, match="byte budget"):
        ReadOnlySQLTool(database).execute(
            {"sql": "SELECT :payload FROM issues", "parameters": {"payload": "x" * (128 * 1024)}},
            CONTEXT,
        )


def test_native_concatenation_limit_rejects_oversized_intermediate(database):
    # Each input is individually below the limit; the intermediate is not.
    with pytest.raises(ToolPolicyViolation, match="byte budget"):
        ReadOnlySQLTool(database).execute(
            {
                "sql": "SELECT :value || :value FROM issues",
                "parameters": {"value": "x" * (40 * 1024)},
            },
            CONTEXT,
        )


def test_sql_text_limit(database):
    with pytest.raises(ToolPolicyViolation, match="SQL text byte budget"):
        ReadOnlySQLTool(database).execute(
            {"sql": "SELECT value FROM issues /*" + "x" * 65536 + "*/"}, CONTEXT
        )


def test_like_pattern_limit_and_reuse(database):
    tool = ReadOnlySQLTool(database)
    with pytest.raises(ToolPolicyViolation):
        tool.execute(
            {
                "sql": "SELECT value FROM issues WHERE value LIKE :pattern",
                "parameters": {"pattern": "x" * 1025},
            },
            CONTEXT,
        )
    assert (
        tool.execute(
            {
                "sql": "SELECT value FROM issues WHERE value LIKE :pattern",
                "parameters": {"pattern": "1"},
            },
            CONTEXT,
        )["row_count"]
        == 1
    )
