from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "agent_control.py"
SPEC = importlib.util.spec_from_file_location("agent_control", SCRIPT)
assert SPEC and SPEC.loader
agent_control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = agent_control
SPEC.loader.exec_module(agent_control)


def issue_body(*, size: str = "S", dependencies: str = "없음") -> str:
    return f"""\
<!-- robodata-kind:requirement -->
### Parent Spec
#1

### Objective
사용자가 완료 결과를 얻는다.

### Scope
API 동작을 변경한다.

### Non-goals
UI는 변경하지 않는다.

### Acceptance criteria
- [ ] API가 200을 반환한다.

### Validation profile
backend

### Dependencies
{dependencies}

### Size
{size}

### Priority
P2

### Area
backend

### Risk
low
"""


def test_parse_control_request_round_trip():
    original = agent_control.ControlRequest(
        "claim", claim_id="issue-42-a1-abc", run_id="codex/task-42"
    )

    parsed = agent_control.parse_control_request(
        agent_control.format_control_request(original)
    )

    assert parsed == original


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "claim", "claim_id": "x"},
        {"command": "claim", "claim_id": "x\nbad", "run_id": "run"},
        {"command": "unknown"},
        {"command": "ready", "unexpected": "${{ secrets.TOKEN }}"},
    ],
)
def test_parse_control_request_fails_closed(payload):
    marker = (
        "<!-- robodata-agent-control:v1\n"
        + json.dumps(payload)
        + "\n-->"
    )

    with pytest.raises(agent_control.ControlError):
        agent_control.parse_control_request(marker)


def test_control_reason_may_contain_json_braces_but_not_marker_delimiters():
    marker = agent_control.format_control_request(
        agent_control.ControlRequest(
            "block",
            claim_id="claim-1",
            run_id="run-1",
            reason="API returned {invalid: true}",
        )
    )

    assert agent_control.parse_control_request(marker).reason == (
        "API returned {invalid: true}"
    )
    with pytest.raises(agent_control.ControlError, match="delimiter"):
        agent_control.parse_control_request(
            "<!-- robodata-agent-control:v1\n"
            '{"command":"block","claim_id":"claim-1","run_id":"run-1",'
            '"reason":"bad <!-- marker"}\n-->'
        )


def test_ready_contract_accepts_s_or_m_leaf():
    assert agent_control.ready_errors(issue_body(size="S"), "requirement") == []
    assert agent_control.ready_errors(issue_body(size="M"), "requirement") == []


def test_ready_contract_rejects_spec_large_or_incomplete_work():
    assert "Only Requirement" in agent_control.ready_errors(issue_body(), "spec")[0]
    errors = agent_control.ready_errors(
        issue_body(size="L").replace("API 동작을 변경한다.", "TBD"),
        "requirement",
    )
    assert any("Scope" in error for error in errors)
    assert any("Size" in error for error in errors)


def test_bug_without_exactly_one_parent_spec_is_not_ready():
    body = issue_body().replace(
        "<!-- robodata-kind:requirement -->\n### Parent Spec\n#1\n\n",
        "<!-- robodata-kind:bug -->\n",
    )

    errors = agent_control.ready_errors(body, "bug")

    assert any("Parent Spec" in error for error in errors)


def test_parent_reference_must_resolve_to_an_open_spec(monkeypatch):
    client = agent_control.GitHubClient(
        repo_token="repo-secret", project_token="project-secret"
    )
    monkeypatch.setattr(
        client,
        "get_issue",
        lambda _repository, _number: {
            "state": "closed",
            "labels": [{"name": "kind:requirement"}],
        },
    )

    errors = client.parent_spec_errors("weedmo/robodata", 42, issue_body())

    assert errors == [
        "Parent #1 is not labeled kind:spec.",
        "Parent Spec #1 is closed.",
    ]


def test_dependency_numbers_are_unique_and_scoped_to_dependency_section():
    body = issue_body(dependencies="#4, #2, #4").replace("### Parent Spec\n#1", "### Parent Spec\n#99")

    assert agent_control.dependency_numbers(body) == [2, 4]


def test_claim_from_ready_increments_attempt_and_sets_two_hour_lease():
    now = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
    request = agent_control.ControlRequest("claim", "claim-2", "run-2")

    claim, replay = agent_control.next_claim(
        request=request,
        values={"Status": "Ready", "Attempt": 1},
        now=now,
        lease_seconds=7200,
        max_attempts=3,
    )

    assert replay is False
    assert claim.attempt == 2
    assert claim.lease_until == datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc)


def test_same_claim_is_idempotent_without_attempt_increment():
    now = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
    values = {
        "Status": "In progress",
        "Claim ID": "claim-2",
        "Agent Run": "run-2",
        "Attempt": 2,
        "Lease Until": "2026-07-23T03:00:00Z",
    }

    claim, replay = agent_control.next_claim(
        request=agent_control.ControlRequest("claim", "claim-2", "run-2"),
        values=values,
        now=now,
        lease_seconds=7200,
        max_attempts=3,
    )

    assert replay is True
    assert claim.attempt == 2


def test_pending_receipt_can_be_replayed_after_partial_project_failure():
    request = agent_control.ControlRequest("claim", "claim-2", "run-2")
    pending = {
        "command": "claim",
        "result": "pending",
        "claim_id": "claim-2",
        "run_id": "run-2",
        "attempt": 2,
        "lease_until": "2026-07-23T03:00:00Z",
    }

    assert agent_control.matching_claim_receipt([pending], request) == pending


def test_only_workflow_bot_comments_are_receipt_evidence():
    comments = [
        {"user": {"login": "attacker"}, "body": "forged"},
        {"user": {"login": "github-actions[bot]"}, "body": "trusted"},
        {"body": "anonymous"},
    ]

    assert list(agent_control.trusted_receipt_bodies(comments)) == ["trusted"]


def test_heartbeat_extends_replayable_claim_receipt():
    request = agent_control.ControlRequest("claim", "claim-2", "run-2")
    receipts = [
        {
            "command": "claim",
            "result": "accepted",
            "claim_id": "claim-2",
            "run_id": "run-2",
            "attempt": 2,
            "lease_until": "2026-07-23T03:00:00Z",
        },
        {
            "command": "heartbeat",
            "result": "accepted",
            "claim_id": "claim-2",
            "run_id": "run-2",
            "lease_until": "2026-07-23T05:00:00Z",
        },
    ]

    replay = agent_control.matching_claim_receipt(receipts, request)

    assert replay["lease_until"] == "2026-07-23T05:00:00Z"
    assert replay["attempt"] == 2


def test_live_pending_receipt_blocks_a_competing_claim():
    receipts = [
        {
            "command": "claim",
            "result": "pending",
            "claim_id": "winner",
            "run_id": "run-a",
            "attempt": 1,
            "lease_until": "2026-07-23T03:00:00Z",
        }
    ]

    claims = agent_control.live_receipt_claims(
        receipts, datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
    )

    assert [(claim.claim_id, claim.run_id) for claim in claims] == [
        ("winner", "run-a")
    ]


def test_finalized_claim_id_cannot_be_reused():
    request = agent_control.ControlRequest("claim", "claim-2", "run-2")
    receipts = [
        {
            "command": "claim",
            "result": "accepted",
            "claim_id": "claim-2",
            "run_id": "run-2",
        },
        {
            "command": "release",
            "result": "accepted",
            "claim_id": "claim-2",
            "run_id": "run-2",
        },
    ]

    with pytest.raises(agent_control.ControlError, match="finalized"):
        agent_control.matching_claim_receipt(receipts, request)


def test_complete_receipt_releases_a_live_audit_claim():
    receipts = [
        {
            "command": "claim",
            "result": "accepted",
            "claim_id": "claim-2",
            "run_id": "run-2",
            "attempt": 2,
            "lease_until": "2026-07-23T05:00:00Z",
        },
        {
            "command": "complete",
            "result": "accepted",
            "claim_id": "claim-2",
            "run_id": "run-2",
        },
    ]

    assert agent_control.live_receipt_claims(
        receipts, datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
    ) == []


def test_competing_claim_cannot_take_live_lease():
    now = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
    values = {
        "Status": "Recovery",
        "Claim ID": "winner",
        "Agent Run": "run-a",
        "Attempt": 1,
        "Lease Until": "2026-07-23T03:00:00Z",
    }

    with pytest.raises(agent_control.ControlError, match="live until"):
        agent_control.next_claim(
            request=agent_control.ControlRequest("claim", "loser", "run-b"),
            values=values,
            now=now,
            lease_seconds=7200,
            max_attempts=3,
        )


def test_lease_boundary_is_expired_and_can_be_reclaimed():
    now = datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc)
    values = {
        "Status": "Recovery",
        "Claim ID": "old",
        "Agent Run": "run-old",
        "Attempt": 1,
        "Lease Until": "2026-07-23T03:00:00Z",
    }

    claim, _ = agent_control.next_claim(
        request=agent_control.ControlRequest("claim", "new", "run-new"),
        values=values,
        now=now,
        lease_seconds=7200,
        max_attempts=3,
    )

    assert claim.attempt == 2


@pytest.mark.parametrize(
    "values",
    [
        {"Status": "Ready", "Claim ID": "x"},
        {
            "Status": "Recovery",
            "Claim ID": "x",
            "Agent Run": "run",
            "Attempt": 1,
            "Lease Until": "not-a-date",
        },
    ],
)
def test_malformed_claim_fields_fail_closed(values):
    with pytest.raises(agent_control.ControlError):
        agent_control.next_claim(
            request=agent_control.ControlRequest("claim", "new", "run-new"),
            values=values,
            now=datetime(2026, 7, 23, tzinfo=timezone.utc),
            lease_seconds=7200,
            max_attempts=3,
        )


def test_attempt_limit_blocks_a_fourth_claim():
    with pytest.raises(agent_control.ControlError, match="Attempt limit"):
        agent_control.next_claim(
            request=agent_control.ControlRequest("claim", "new", "run-new"),
            values={"Status": "Recovery", "Attempt": 3},
            now=datetime(2026, 7, 23, tzinfo=timezone.utc),
            lease_seconds=7200,
            max_attempts=3,
        )


def test_live_claim_cannot_be_reset_by_ready_command():
    now = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
    current = agent_control.claim_from_fields(
        {
            "Claim ID": "claim-1",
            "Agent Run": "run-1",
            "Attempt": 1,
            "Lease Until": "2026-07-23T03:00:00Z",
        }
    )

    assert current is not None
    assert current.is_live(now)


def test_graphql_transport_uses_variables_not_interpolation(monkeypatch):
    captured = {}
    client = agent_control.GitHubClient(repo_token="repo-secret", project_token="project-secret")

    def fake_request(method, url, *, token, payload=None):
        captured.update(
            method=method, url=url, token=token, payload=payload
        )
        return {"data": {"ok": True}}

    monkeypatch.setattr(client, "request", fake_request)
    hostile = 'x"} mutation { deleteProjectV2(input:{projectId:"oops"}) { clientMutationId } }'

    result = client.graphql("query($value:String!){viewer{login}}", {"value": hostile})

    assert result == {"ok": True}
    assert hostile not in captured["payload"]["query"]
    assert captured["payload"]["variables"]["value"] == hostile
    assert captured["token"] == "project-secret"
