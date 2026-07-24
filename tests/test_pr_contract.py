from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "pr_contract.py"
SPEC = importlib.util.spec_from_file_location("pr_contract", SCRIPT)
assert SPEC and SPEC.loader
pr_contract = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pr_contract
SPEC.loader.exec_module(pr_contract)


def valid_body() -> str:
    return """\
Closes #42
Relates #7

## Summary
작업 결과를 요약합니다.

## Scope
- 포함: API
- 제외: UI

## Acceptance criteria
- [x] 요청이 처리된다.

## Validation
`pytest -q tests/test_api.py` 통과

## Validation gaps
실데이터 검증은 fixture 부재로 생략

## Data safety
원본 데이터는 읽기 전용

## Submodule changes
없음

## Risk and rollback
낮음. PR revert로 복구

## Agent metadata
Claim-ID: claim-42
Agent-Run: codex/task-123
"""


def test_valid_contract_is_parsed():
    contract, errors = pr_contract.validate_pr_contract(
        body=valid_body(),
        head_ref="agent/42-a2-add-search",
    )

    assert errors == []
    assert contract.issue_number == 42
    assert contract.attempt == 2
    assert contract.claim_id == "claim-42"
    assert contract.related_spec == 7


def test_contract_rejects_branch_and_closing_issue_mismatch():
    contract, errors = pr_contract.validate_pr_contract(
        body=valid_body(),
        head_ref="agent/41-a2-add-search",
    )

    assert contract is None
    assert any("does not match" in error for error in errors)


def test_contract_requires_one_leaf_and_all_nonempty_sections():
    body = valid_body().replace("Closes #42", "Closes #42\nFixes #43")
    body = body.replace("작업 결과를 요약합니다.", "<!-- required -->")

    contract, errors = pr_contract.validate_pr_contract(
        body=body,
        head_ref="agent/42-a2-add-search",
    )

    assert contract is None
    assert any("exactly one" in error for error in errors)
    assert any("Summary" in error and "empty" in error for error in errors)


def test_contract_requires_all_acceptance_criteria_checked():
    body = valid_body().replace("- [x] 요청이 처리된다.", "- [ ] 요청이 처리된다.")

    contract, errors = pr_contract.validate_pr_contract(
        body=body,
        head_ref="agent/42-a2-add-search",
    )

    assert contract is None
    assert any("acceptance criterion" in error for error in errors)


def test_remote_contract_requires_leaf_active_claim_and_no_competing_pr():
    contract, errors = pr_contract.validate_pr_contract(
        body=valid_body(),
        head_ref="agent/42-a2-add-search",
    )
    assert errors == []
    receipt = {
        "command": "claim",
        "result": "accepted",
        "claim_id": "claim-42",
        "run_id": "codex/task-123",
        "attempt": 2,
        "lease_until": "2026-07-23T03:00:00Z",
    }
    comments = [
        {
            "user": {"login": "github-actions[bot]"},
            "body": (
                "<!-- robodata-agent-receipt:v1\n"
                + json.dumps(receipt)
                + "\n-->"
            )
        }
    ]

    remote_errors = pr_contract.validate_remote_contract(
        contract=contract,
        issue={
            "state": "open",
            "labels": [{"name": "kind:requirement"}],
            "body": "### Parent Spec\n#7",
        },
        comments=comments,
        open_pulls=[
            {"number": 55, "body": "Closes #42"},
            {"number": 56, "body": "Closes #42"},
        ],
        current_pr_number=55,
        related_spec_issue={
            "state": "open",
            "labels": [{"name": "kind:spec"}],
        },
        now=datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc),
    )

    assert remote_errors == ["Leaf issue already has another open PR: #56."]


def test_remote_contract_rejects_untrusted_or_expired_claim_receipts():
    contract, errors = pr_contract.validate_pr_contract(
        body=valid_body(),
        head_ref="agent/42-a2-add-search",
    )
    assert errors == []
    receipt = {
        "command": "claim",
        "result": "accepted",
        "claim_id": "claim-42",
        "run_id": "codex/task-123",
        "attempt": 2,
        "lease_until": "2026-07-23T02:00:00Z",
    }

    def validate(author: str, now: datetime):
        return pr_contract.validate_remote_contract(
            contract=contract,
            issue={
                "state": "open",
                "labels": [{"name": "kind:requirement"}],
                "body": "### Parent Spec\n#7",
            },
            comments=[
                {
                    "user": {"login": author},
                    "body": (
                        "<!-- robodata-agent-receipt:v1\n"
                        + json.dumps(receipt)
                        + "\n-->"
                    ),
                }
            ],
            open_pulls=[{"number": 55, "body": "Closes #42"}],
            current_pr_number=55,
            related_spec_issue={
                "state": "open",
                "labels": [{"name": "kind:spec"}],
            },
            now=now,
        )

    assert any(
        "trusted active" in error
        for error in validate(
            "untrusted-user", datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
        )
    )
    assert any(
        "expired" in error
        for error in validate(
            "github-actions[bot]",
            datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
        )
    )


def test_parent_spec_parser_is_section_scoped_and_unambiguous():
    assert pr_contract._issue_parent_spec(
        "### Parent Spec\n#7\n\n### Dependencies\n#8"
    ) == 7
    assert (
        pr_contract._issue_parent_spec(
            "### Parent Spec\n\n### Dependencies\n#7"
        )
        is None
    )
    assert pr_contract._issue_parent_spec("### Parent Spec\n#7 and #8") is None


def test_cli_rejects_non_pull_request_event(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"issue": {"number": 1}}), encoding="utf-8")

    assert pr_contract.main(["--event", str(event)]) == 2
