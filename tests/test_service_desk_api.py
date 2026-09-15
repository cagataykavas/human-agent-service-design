from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from service_desk.api import create_service_desk_router
from service_desk.errors import ServiceDeskError
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.service import ServiceDesk


def client_for(tmp_path: Path) -> TestClient:
    desk = ServiceDesk(SQLiteServiceDeskRepository(tmp_path / "api.db"))
    desk.bootstrap()
    app = FastAPI()

    @app.exception_handler(ServiceDeskError)
    async def domain_error(_: Request, exc: ServiceDeskError):
        return JSONResponse(
            status_code=409,
            content={"error": type(exc).__name__, "detail": str(exc)},
        )

    app.include_router(create_service_desk_router(desk))
    return TestClient(app)


def test_api_exposes_issue_lifecycle_and_agent_job(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    project = client.post("/v1/projects", json={"key": "OPS", "name": "Operations"})
    assert project.status_code == 201
    project_id = project.json()["project_id"]
    headers = {
        "x-actor-id": "user-1",
        "x-actor-role": "requester",
        "idempotency-key": "client-request-1",
    }
    issue = client.post(
        "/v1/issues",
        headers=headers,
        json={
            "project_id": project_id,
            "summary": "VPN unavailable",
            "description": "Cannot connect from home.",
            "priority": "high",
            "labels": ["vpn"],
        },
    )
    assert issue.status_code == 201
    payload = issue.json()
    assert payload["issue_key"] == "OPS-1"

    triaged = client.post(
        f"/v1/issues/{payload['issue_id']}/transitions",
        headers={"x-actor-id": "triage-worker", "x-actor-role": "ai_worker"},
        json={"name": "triage", "expected_version": 1},
    )
    assert triaged.status_code == 200
    assert triaged.json()["status"] == "triaged"

    job = client.post(
        f"/v1/issues/{payload['issue_id']}/agent-jobs",
        headers={"x-actor-id": "agent-1", "x-actor-role": "agent"},
        json={"capability": "summarize", "input_payload": {"text": "VPN unavailable"}},
    )
    assert job.status_code == 202
    assert job.json()["state"] == "queued"
    audit = client.get(f"/v1/issues/{payload['issue_id']}/audit")
    assert [event["event_type"] for event in audit.json()] == [
        "issue_created",
        "issue_transitioned",
    ]


def test_api_exposes_safe_filtered_issue_search(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    project_id = client.post("/v1/projects", json={"key": "OPS", "name": "Operations"}).json()[
        "project_id"
    ]
    for number, summary in enumerate(("VPN authentication fails", "Laptop replacement"), start=1):
        response = client.post(
            "/v1/issues",
            headers={
                "x-actor-id": "user-1",
                "x-actor-role": "requester",
                "idempotency-key": f"request-{number}",
            },
            json={
                "project_id": project_id,
                "summary": summary,
                "priority": "high" if number == 1 else "low",
                "labels": ["vpn"] if number == 1 else ["hardware"],
            },
        )
        assert response.status_code == 201
    search = client.get(
        f"/v1/projects/{project_id}/search",
        params={"q": 'priority = high AND text ~ "authentication"', "limit": 1},
    )
    assert search.status_code == 200
    assert [item["issue_key"] for item in search.json()["items"]] == ["OPS-1"]
    rejected = client.get(
        f"/v1/projects/{project_id}/search", params={"q": "status = open OR 1 = 1"}
    )
    assert rejected.status_code == 409
