"""Low-cardinality operational metrics derived from durable service-desk state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from service_desk.repository import SQLiteServiceDeskRepository


@dataclass(frozen=True, slots=True)
class OperationalSnapshot:
    issues_by_status: dict[str, int]
    agent_jobs_by_state: dict[str, int]
    outbox_pending: int
    outbox_oldest_age_seconds: float
    sla_first_response_breached: int
    sla_resolution_breached: int
    sla_at_risk: int


def collect_snapshot(
    repository: SQLiteServiceDeskRepository,
    *,
    risk_horizon_seconds: int = 1800,
) -> OperationalSnapshot:
    if not 60 <= risk_horizon_seconds <= 86_400:
        raise ValueError("risk horizon must be in [60, 86400] seconds")
    now = repository.now()
    now_text = repository.time(now)
    horizon_text = repository.time(now + timedelta(seconds=risk_horizon_seconds))
    with repository.connect() as connection:
        issues = connection.execute(
            "SELECT status, COUNT(*) AS count FROM issues GROUP BY status"
        ).fetchall()
        jobs = connection.execute(
            "SELECT state, COUNT(*) AS count FROM agent_jobs GROUP BY state"
        ).fetchall()
        outbox = connection.execute(
            """
            SELECT COUNT(*) AS count, MIN(created_at) AS oldest
            FROM outbox_events WHERE published_at IS NULL
            """
        ).fetchone()
        sla = connection.execute(
            """
            SELECT
                SUM(CASE WHEN first_responded_at IS NULL AND paused_at IS NULL
                    AND first_response_due_at < :now THEN 1 ELSE 0 END) AS first_breached,
                SUM(CASE WHEN resolved_at IS NULL AND paused_at IS NULL
                    AND resolution_due_at < :now THEN 1 ELSE 0 END) AS resolution_breached,
                SUM(CASE WHEN resolved_at IS NULL AND paused_at IS NULL
                    AND resolution_due_at >= :now AND resolution_due_at <= :horizon
                    THEN 1 ELSE 0 END) AS at_risk
            FROM sla_instances
            """,
            {"now": now_text, "horizon": horizon_text},
        ).fetchone()
    oldest_age = 0.0
    if outbox["oldest"]:
        oldest_age = max(0.0, (now - datetime.fromisoformat(outbox["oldest"])).total_seconds())
    return OperationalSnapshot(
        issues_by_status={row["status"]: row["count"] for row in issues},
        agent_jobs_by_state={row["state"]: row["count"] for row in jobs},
        outbox_pending=outbox["count"],
        outbox_oldest_age_seconds=oldest_age,
        sla_first_response_breached=sla["first_breached"] or 0,
        sla_resolution_breached=sla["resolution_breached"] or 0,
        sla_at_risk=sla["at_risk"] or 0,
    )


def render_prometheus(snapshot: OperationalSnapshot) -> str:
    lines = [
        "# HELP service_desk_issues Current issues by workflow status.",
        "# TYPE service_desk_issues gauge",
    ]
    for status, count in sorted(snapshot.issues_by_status.items()):
        lines.append(f'service_desk_issues{{status="{_label(status)}"}} {count}')
    lines.extend(
        (
            "# HELP service_desk_agent_jobs Current durable agent jobs by state.",
            "# TYPE service_desk_agent_jobs gauge",
        )
    )
    for state, count in sorted(snapshot.agent_jobs_by_state.items()):
        lines.append(f'service_desk_agent_jobs{{state="{_label(state)}"}} {count}')
    lines.extend(
        (
            "# HELP service_desk_outbox_pending Unpublished transactional outbox rows.",
            "# TYPE service_desk_outbox_pending gauge",
            f"service_desk_outbox_pending {snapshot.outbox_pending}",
            "# HELP service_desk_outbox_oldest_age_seconds Age of oldest unpublished event.",
            "# TYPE service_desk_outbox_oldest_age_seconds gauge",
            f"service_desk_outbox_oldest_age_seconds {snapshot.outbox_oldest_age_seconds:.3f}",
            "# HELP service_desk_sla_breached Current open SLA breaches by objective.",
            "# TYPE service_desk_sla_breached gauge",
            (
                'service_desk_sla_breached{objective="first_response"} '
                f"{snapshot.sla_first_response_breached}"
            ),
            (
                'service_desk_sla_breached{objective="resolution"} '
                f"{snapshot.sla_resolution_breached}"
            ),
            "# HELP service_desk_sla_at_risk Open resolution SLAs inside the risk horizon.",
            "# TYPE service_desk_sla_at_risk gauge",
            f"service_desk_sla_at_risk {snapshot.sla_at_risk}",
        )
    )
    return "\n".join(lines) + "\n"


def database_ready(repository: SQLiteServiceDeskRepository) -> bool:
    try:
        with repository.connect() as connection:
            connection.execute("SELECT 1 FROM workflows LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001 - health boundary must turn adapter failures into not-ready
        return False
    return True


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
