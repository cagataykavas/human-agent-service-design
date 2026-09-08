from fastapi.testclient import TestClient
from app.api import app


client = TestClient(app)


def test_low_impact_high_confidence_case_can_automate() -> None:
    payload = {
        "case": {
            "case_id": "case-api-1",
            "customer_segment": "retail",
            "impact": "low",
            "evidence": [
                {
                    "evidence_id": "id-1",
                    "kind": "identity",
                    "source": "document",
                    "quality": 0.97,
                },
                {
                    "evidence_id": "addr-1",
                    "kind": "address",
                    "source": "registry",
                    "quality": 0.95,
                },
            ],
            "agent": {
                "action": "approve",
                "confidence": 0.95,
                "reason_codes": ["identity_match", "address_match"],
                "explanation": "The supplied evidence satisfies the synthetic policy.",
                "evidence_ids": ["id-1", "addr-1"],
            },
        }
    }
    response = client.post("/route", json=payload)
    assert response.status_code == 200
    assert response.json()["decision"]["route"] == "automate"


def test_high_impact_case_is_deferred_to_human() -> None:
    payload = {
        "case": {
            "case_id": "case-api-2",
            "customer_segment": "business",
            "impact": "high",
            "evidence": [
                {
                    "evidence_id": "ubo-1",
                    "kind": "ownership",
                    "source": "registry",
                    "quality": 0.94,
                }
            ],
            "agent": {
                "action": "approve",
                "confidence": 0.99,
                "reason_codes": ["ownership_match"],
                "explanation": "Evidence appears internally consistent.",
                "evidence_ids": ["ubo-1"],
            },
        }
    }
    response = client.post("/route", json=payload)
    assert response.status_code == 200
    assert response.json()["decision"]["route"] == "human_review"
    assert "high_impact_decision" in response.json()["decision"]["reasons"]


def test_experiment_metrics_expose_override_rate() -> None:
    response = client.post(
        "/experiments/metrics",
        json={
            "outcomes": [
                {
                    "case_id": "a",
                    "agent_action": "approve",
                    "reviewer_action": "reject",
                    "route": "human_review",
                    "decision_seconds": 80,
                    "customer_loops": 0,
                    "explanation_present": True,
                },
                {
                    "case_id": "b",
                    "agent_action": "approve",
                    "reviewer_action": "approve",
                    "route": "automate",
                    "decision_seconds": 2,
                    "customer_loops": 0,
                    "explanation_present": True,
                },
            ]
        },
    )
    assert response.status_code == 200
    metrics = response.json()
    assert metrics["cases"] == 2
    assert metrics["automation_rate"] == 0.5
    assert metrics["human_review_rate"] == 0.5
    assert metrics["override_rate"] == 1.0
