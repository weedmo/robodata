"""Static contracts for the repository's agent-only GitHub control plane."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GITHUB = ROOT / ".github"
FORM_HEADINGS = (
    "Objective",
    "Scope",
    "Non-goals",
    "Acceptance criteria",
    "Validation profile",
    "Dependencies",
    "Size",
    "Priority",
    "Area",
    "Risk",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_issue_forms_are_closed_and_machine_classifiable():
    config = read(GITHUB / "ISSUE_TEMPLATE" / "config.yml")
    assert "blank_issues_enabled: false" in config
    assert "/security/advisories/new" in config

    expected = {
        "spec.yml": ("robodata-kind:spec", '"kind:spec"'),
        "requirement.yml": ("robodata-kind:requirement", '"kind:requirement"'),
        "bug.yml": ("robodata-kind:bug", '"bug"'),
    }
    for filename, (marker, kind_label) in expected.items():
        text = read(GITHUB / "ISSUE_TEMPLATE" / filename)
        assert marker in text
        assert kind_label in text
        assert '"needs-triage"' in text
        ids = re.findall(r"(?m)^\s+id:\s*(\S+)\s*$", text)
        assert len(ids) == len(set(ids))
        for heading in FORM_HEADINGS:
            assert f"label: {heading}" in text


def test_pr_template_matches_the_enforced_contract():
    template = read(GITHUB / "pull_request_template.md")
    assert re.search(r"(?m)^Closes #", template)
    assert re.search(r"(?m)^Relates to #", template)
    for heading in (
        "Summary",
        "Scope",
        "Acceptance criteria",
        "Validation",
        "Validation gaps",
        "Data safety",
        "Submodule changes",
        "Risk and rollback",
        "Agent metadata",
    ):
        assert f"## {heading}" in template
    assert "Claim-ID:" in template
    assert "Agent-Run:" in template


def test_project_declares_dispatch_and_recovery_views_without_runtime_ids():
    manifest = json.loads(read(GITHUB / "project.json"))
    serialized = json.dumps(manifest).lower()
    assert "token" not in serialized
    assert all("id" not in field for field in manifest["fields"])
    views = {view["name"]: view for view in manifest["views"]}
    assert set(views) == {
        "Dispatch",
        "Active",
        "Review",
        "Recovery",
        "Specs",
        "Release",
    }
    assert 'status:"Ready","Recovery"' in views["Dispatch"]["filter"]


def test_workflows_use_least_privilege_timeouts_and_pinned_actions():
    workflows = sorted((GITHUB / "workflows").glob("*.yml"))
    assert {path.name for path in workflows} == {
        "agent-control.yml",
        "bootstrap-github-operations.yml",
        "ci.yml",
        "pr-contract.yml",
    }
    action_ref = re.compile(r"(?m)^\s*uses:\s*[^@\s]+@([0-9a-f]{40})(?:\s+#.*)?$")
    for path in workflows:
        text = read(path)
        assert "\npermissions:\n" in text
        assert "write-all" not in text
        assert "pull_request_target" not in text
        assert text.count("timeout-minutes:") >= text.count("runs-on:")
        uses = re.findall(r"(?m)^\s*uses:\s*(\S+)", text)
        assert uses
        assert len(action_ref.findall(text)) == len(uses)


def test_ci_and_control_workflows_execute_real_guards():
    ci = read(GITHUB / "workflows" / "ci.yml")
    assert "uv run pytest -q tests" in ci
    assert "npm run build" in ci
    assert "docker compose -f docker/compose.yml up -d db" in ci
    assert "docker build --file docker/curation-worker/Dockerfile" in ci
    assert "docker run --rm --entrypoint python curation-worker:test" in ci
    assert "scripts/verify_no_host_control.sh" in ci
    assert "needs: [policy, backend, frontend, curation_worker_image]" in ci
    assert "token: ${{ secrets.SUBMODULE_PAT }}" in ci

    control = read(GITHUB / "workflows" / "agent-control.yml")
    assert "cancel-in-progress: false" in control
    assert "queue: max" in control
    assert "PROJECTS_CLASSIC_PAT" in control
    assert "PROJECT_OWNER" in control
    assert "PROJECT_NUMBER" in control
    assert "ref: ${{ github.event.repository.default_branch }}" in control

    pr_contract = read(GITHUB / "workflows" / "pr-contract.yml")
    assert "ref: ${{ github.event.pull_request.base.sha }}" in pr_contract


def test_governance_has_no_external_task_tracker_reference():
    forbidden_tracker = "asa" + "na"
    paths = [
        ROOT / "AGENTS.md",
        ROOT / ".agents" / "skills" / "to-prd" / "SKILL.md",
        ROOT / "docs" / "engineering" / "agent-operations.md",
        *GITHUB.rglob("*"),
    ]
    for path in paths:
        if path.is_file() and path.suffix in {".md", ".json", ".py", ".yml", ".yaml"}:
            assert forbidden_tracker not in read(path).lower()
