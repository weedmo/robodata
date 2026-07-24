#!/usr/bin/env python3
"""Validate the machine-readable contract for an agent-authored pull request."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CLOSING_RE = re.compile(
    r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(?P<number>\d+)\s*$"
)
RELATES_RE = re.compile(r"(?im)^\s*relates(?:\s+to)?\s+#(?P<number>\d+)\s*$")
BRANCH_RE = re.compile(
    r"^agent/(?P<issue>\d+)-a(?P<attempt>[1-9]\d*)-[a-z0-9]+(?:-[a-z0-9]+)*$"
)
METADATA_RE = {
    "Claim-ID": re.compile(r"(?im)^\s*Claim-ID:\s*(?P<value>\S+)\s*$"),
    "Agent-Run": re.compile(r"(?im)^\s*Agent-Run:\s*(?P<value>\S+)\s*$"),
}
REQUIRED_SECTIONS = (
    "Summary",
    "Scope",
    "Acceptance criteria",
    "Validation",
    "Validation gaps",
    "Data safety",
    "Submodule changes",
    "Risk and rollback",
    "Agent metadata",
)
PLACEHOLDERS = {
    "",
    "-",
    "tbd",
    "todo",
    "_no response_",
    "<!-- required -->",
}


@dataclass(frozen=True)
class PullRequestContract:
    issue_number: int
    attempt: int
    claim_id: str
    agent_run: str
    related_spec: int | None


def _sections(body: str) -> dict[str, str]:
    """Return H2 sections without depending on a Markdown parser."""
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", body))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        result[match.group(1).strip()] = body[match.end() : end].strip()
    return result


def _issue_parent_spec(body: str) -> int | None:
    matches = list(re.finditer(r"(?m)^###\s+(.+?)\s*$", body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group(1).strip()] = body[match.end() : end].strip()
    numbers = {
        int(number)
        for number in re.findall(
            r"(?<!\w)#(\d+)\b", sections.get("Parent Spec", "")
        )
    }
    if len(numbers) != 1:
        return None
    return numbers.pop()


def _is_filled(value: str) -> bool:
    without_comments = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL).strip()
    return without_comments.lower() not in PLACEHOLDERS


def validate_pr_contract(
    *,
    body: str,
    head_ref: str,
    base_ref: str = "main",
) -> tuple[PullRequestContract | None, list[str]]:
    """Validate a PR body and branch, returning a parsed contract and errors."""
    errors: list[str] = []
    if base_ref != "main":
        errors.append("PR base must be main.")

    closing_numbers = [int(match.group("number")) for match in CLOSING_RE.finditer(body)]
    if len(closing_numbers) != 1:
        errors.append("PR body must contain exactly one standalone `Closes #<leaf>` line.")

    branch = BRANCH_RE.fullmatch(head_ref)
    if branch is None:
        errors.append("Branch must match `agent/<issue>-a<attempt>-<slug>`.")

    sections = _sections(body)
    for name in REQUIRED_SECTIONS:
        if name not in sections:
            errors.append(f"Missing required section: `## {name}`.")
        elif not _is_filled(sections[name]):
            errors.append(f"Required section is empty: `## {name}`.")

    metadata: dict[str, str] = {}
    for name, pattern in METADATA_RE.items():
        matches = list(pattern.finditer(body))
        if len(matches) != 1:
            errors.append(f"`{name}: <value>` must appear exactly once.")
        else:
            metadata[name] = matches[0].group("value")

    related = [int(match.group("number")) for match in RELATES_RE.finditer(body)]
    if len(related) != 1:
        errors.append(
            "PR body must contain exactly one standalone `Relates to #<spec>` line."
        )

    acceptance = sections.get("Acceptance criteria", "")
    if acceptance and (
        re.search(r"(?im)^\s*-\s*\[\s\]", acceptance)
        or not re.search(r"(?im)^\s*-\s*\[[xX]\]", acceptance)
    ):
        errors.append("Every acceptance criterion must be checked.")

    if branch is not None and len(closing_numbers) == 1:
        branch_issue = int(branch.group("issue"))
        if branch_issue != closing_numbers[0]:
            errors.append(
                f"Branch issue #{branch_issue} does not match Closes #{closing_numbers[0]}."
            )

    if errors:
        return None, errors

    assert branch is not None
    return (
        PullRequestContract(
            issue_number=closing_numbers[0],
            attempt=int(branch.group("attempt")),
            claim_id=metadata["Claim-ID"],
            agent_run=metadata["Agent-Run"],
            related_spec=related[0] if related else None,
        ),
        [],
    )


def _receipt_payloads(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"<!--\s*robodata-agent-receipt:v1\s*(?P<payload>.*?)\s*-->",
        re.DOTALL,
    )
    result: list[dict[str, Any]] = []
    for comment in comments:
        author = comment.get("user")
        if not isinstance(author, dict) or author.get("login") != "github-actions[bot]":
            continue
        for match in pattern.finditer(comment.get("body") or ""):
            try:
                value = json.loads(match.group("payload"))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result.append(value)
    return result


def _active_claim(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    active: dict[str, Any] | None = None
    for receipt in _receipt_payloads(comments):
        command = receipt.get("command")
        result = receipt.get("result")
        if command == "claim" and result == "accepted":
            active = receipt
        elif (
            command == "heartbeat"
            and result == "accepted"
            and active is not None
            and receipt.get("claim_id") == active.get("claim_id")
            and receipt.get("run_id") == active.get("run_id")
        ):
            active = {**active, "lease_until": receipt.get("lease_until")}
        elif (
            command in {"block", "release", "complete"}
            and result in {"accepted", "reconciled"}
            and active is not None
            and receipt.get("claim_id") == active.get("claim_id")
        ):
            active = None
    return active


def validate_remote_contract(
    *,
    contract: PullRequestContract,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    open_pulls: list[dict[str, Any]],
    current_pr_number: int,
    related_spec_issue: dict[str, Any],
    now: datetime | None = None,
) -> list[str]:
    """Validate leaf and claim facts that are not present in the PR body."""
    errors: list[str] = []
    labels = {
        label.get("name")
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }
    if issue.get("state") != "open":
        errors.append(f"Leaf issue #{contract.issue_number} must be open.")
    if not ({"kind:requirement", "bug"} & labels) or "kind:spec" in labels:
        errors.append(
            f"Issue #{contract.issue_number} is not an executable Requirement/Bug leaf."
        )
    spec_labels = {
        label.get("name")
        for label in related_spec_issue.get("labels", [])
        if isinstance(label, dict)
    }
    if "kind:spec" not in spec_labels:
        errors.append(f"Related issue #{contract.related_spec} is not a Spec.")
    if related_spec_issue.get("state") != "open":
        errors.append(f"Related Spec #{contract.related_spec} must be open.")
    if _issue_parent_spec(issue.get("body") or "") != contract.related_spec:
        errors.append("PR Relates target does not match the leaf's Parent Spec.")

    claim = _active_claim(comments)
    if claim is None:
        errors.append("Leaf issue has no trusted active accepted claim receipt.")
    else:
        if claim.get("claim_id") != contract.claim_id:
            errors.append("PR Claim-ID does not match the active receipt.")
        if claim.get("run_id") != contract.agent_run:
            errors.append("PR Agent-Run does not match the active receipt.")
        try:
            receipt_attempt = int(claim.get("attempt"))
        except (TypeError, ValueError):
            errors.append("Active claim receipt has an invalid attempt.")
        else:
            if receipt_attempt != contract.attempt:
                errors.append("PR branch attempt does not match the active receipt.")
        lease_value = claim.get("lease_until")
        try:
            lease_until = datetime.fromisoformat(str(lease_value).replace("Z", "+00:00"))
            if lease_until.tzinfo is None:
                raise ValueError
        except ValueError:
            errors.append("Active claim receipt has an invalid lease timestamp.")
        else:
            effective_now = now or datetime.now(timezone.utc)
            if lease_until.astimezone(timezone.utc) <= effective_now.astimezone(
                timezone.utc
            ):
                errors.append("Active claim receipt lease has expired.")

    duplicates: list[int] = []
    for pull in open_pulls:
        number = pull.get("number")
        if number == current_pr_number:
            continue
        linked = {
            int(match.group("number"))
            for match in CLOSING_RE.finditer(pull.get("body") or "")
        }
        if contract.issue_number in linked and isinstance(number, int):
            duplicates.append(number)
    if duplicates:
        errors.append(
            "Leaf issue already has another open PR: "
            + ", ".join(f"#{number}" for number in sorted(duplicates))
            + "."
        )
    return errors


class GitHubReader:
    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self.token = token

    def _request(self, path: str) -> Any:
        request = urllib.request.Request(
            f"https://api.github.com{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "robodata-pr-contract/1",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise ValueError(f"GitHub API returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise ValueError(f"GitHub API connection failed: {exc.reason}") from None
        except json.JSONDecodeError as exc:
            raise ValueError("GitHub API returned invalid JSON") from exc

    def issue(self, number: int) -> dict[str, Any]:
        owner, repo = self.repository.split("/", 1)
        value = self._request(f"/repos/{owner}/{repo}/issues/{number}")
        if not isinstance(value, dict):
            raise ValueError("GitHub issue response is malformed")
        return value

    def paginated(self, endpoint: str) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        page = 1
        separator = "&" if "?" in endpoint else "?"
        while True:
            batch = self._request(f"{endpoint}{separator}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ValueError("GitHub list response is malformed")
            values.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                return values
            page += 1

    def evidence(
        self, issue_number: int, related_spec_number: int
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        owner, repo = self.repository.split("/", 1)
        return (
            self.issue(issue_number),
            self.issue(related_spec_number),
            self.paginated(
                f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
            ),
            self.paginated(f"/repos/{owner}/{repo}/pulls?state=open"),
        )


def _load_event(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read GitHub event: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("GitHub event root must be an object")
    return value


def _write_summary(errors: list[str], contract: PullRequestContract | None) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    if errors:
        lines = ["## PR contract: failed", "", *[f"- {error}" for error in errors]]
    else:
        assert contract is not None
        lines = [
            "## PR contract: passed",
            "",
            f"- Leaf issue: #{contract.issue_number}",
            f"- Attempt: {contract.attempt}",
            f"- Claim ID: `{contract.claim_id}`",
        ]
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--event",
        type=Path,
        default=Path(os.environ.get("GITHUB_EVENT_PATH", "")),
        help="GitHub pull_request event JSON",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="validate only the local body/branch shape (for tests and authoring)",
    )
    args = parser.parse_args(argv)

    if not str(args.event):
        print("GITHUB_EVENT_PATH or --event is required", file=sys.stderr)
        return 2
    try:
        event = _load_event(args.event)
        pull_request = event["pull_request"]
        body = pull_request.get("body") or ""
        head_ref = pull_request["head"]["ref"]
        base_ref = pull_request["base"]["ref"]
    except (KeyError, TypeError, ValueError) as exc:
        print(f"invalid pull_request event: {exc}", file=sys.stderr)
        return 2

    contract, errors = validate_pr_contract(
        body=body,
        head_ref=head_ref,
        base_ref=base_ref,
    )
    if not errors and not args.offline:
        token = os.environ.get("GITHUB_TOKEN", "")
        repository = event.get("repository", {}).get("full_name")
        pull_number = event.get("number")
        if not token or not isinstance(repository, str) or not isinstance(pull_number, int):
            errors.append(
                "GITHUB_TOKEN, repository.full_name, and PR number are required "
                "for remote contract validation."
            )
        else:
            assert contract is not None
            try:
                issue, related_spec, comments, pulls = GitHubReader(
                    repository, token
                ).evidence(contract.issue_number, contract.related_spec)
                errors.extend(
                    validate_remote_contract(
                        contract=contract,
                        issue=issue,
                        comments=comments,
                        open_pulls=pulls,
                        current_pr_number=pull_number,
                        related_spec_issue=related_spec,
                    )
                )
            except ValueError as exc:
                errors.append(str(exc))
    _write_summary(errors, contract)
    if errors:
        for error in errors:
            print(f"::error::{error}")
        return 1
    print(json.dumps(contract.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
