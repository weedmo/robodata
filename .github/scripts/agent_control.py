#!/usr/bin/env python3
"""Serialize agent claims and materialize their state in a GitHub Project v2.

Issue comments are the audit log. Project fields are a repairable projection used
for dispatch. The workflow calling this module must use per-issue concurrency.
"""

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


REQUEST_RE = re.compile(
    r"<!--\s*robodata-agent-control:v1\s*(?P<payload>.*?)\s*-->",
    re.DOTALL,
)
RECEIPT_RE = re.compile(
    r"<!--\s*robodata-agent-receipt:v1\s*(?P<payload>.*?)\s*-->",
    re.DOTALL,
)
FIELD_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
CLOSES_RE = re.compile(
    r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(?P<number>\d+)\s*$"
)
CLAIM_ID_RE = re.compile(r"(?im)^\s*Claim-ID:\s*`?(?P<value>[^`\s]+)`?\s*$")
AGENT_RUN_RE = re.compile(r"(?im)^\s*Agent-Run:\s*`?(?P<value>[^`\s]+)`?\s*$")
FORM_HEADING_RE = re.compile(r"(?m)^###\s+(.+?)\s*$")
PLACEHOLDERS = {"", "-", "tbd", "todo", "_no response_", "없음?"}
ALLOWED_COMMANDS = {"ready", "claim", "heartbeat", "block", "release"}
LEAF_KINDS = {"requirement", "bug"}
VALIDATION_PROFILES = {
    "backend",
    "frontend",
    "fullstack",
    "db",
    "docker",
    "docs",
    "submodule",
}
PROJECT_SINGLE_SELECTS = {
    "Type",
    "Priority",
    "Area",
    "Risk",
    "Size",
    "Validation Profile",
    "Status",
}
LEASE_STATUSES = {"In progress", "In review"}


class ControlError(RuntimeError):
    """An expected, sanitized control-plane failure."""


@dataclass(frozen=True)
class ControlRequest:
    command: str
    claim_id: str | None = None
    run_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ClaimState:
    claim_id: str
    run_id: str
    attempt: int
    lease_until: datetime

    def is_live(self, now: datetime) -> bool:
        return self.lease_until > now


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControlError("Lease Until must be a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ControlError("Lease Until must include a timezone.")
    return parsed.astimezone(timezone.utc)


def parse_control_request(comment_body: str) -> ControlRequest | None:
    """Parse a single versioned request marker and reject ambiguous input."""
    matches = list(REQUEST_RE.finditer(comment_body))
    if not matches:
        return None
    if len(matches) != 1:
        raise ControlError("Exactly one robodata-agent-control marker is allowed.")
    if len(comment_body) > 8_192:
        raise ControlError("Agent control comment is too large.")
    try:
        payload = json.loads(matches[0].group("payload"))
    except json.JSONDecodeError as exc:
        raise ControlError("Agent control marker contains invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ControlError("Agent control payload must be a JSON object.")
    unknown = set(payload) - {"command", "claim_id", "run_id", "reason"}
    if unknown:
        raise ControlError(f"Unsupported control keys: {', '.join(sorted(unknown))}.")

    command = payload.get("command")
    if command not in ALLOWED_COMMANDS:
        raise ControlError(f"Unsupported agent command: {command!r}.")

    def identifier(name: str) -> str | None:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not FIELD_VALUE_RE.fullmatch(value):
            raise ControlError(f"{name} has an invalid format.")
        return value

    claim_id = identifier("claim_id")
    run_id = identifier("run_id")
    reason = payload.get("reason")
    if reason is not None:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ControlError("reason must be 1-500 characters.")
        if "\x00" in reason or "<!--" in reason or "-->" in reason:
            raise ControlError("reason contains an invalid marker delimiter.")
        reason = reason.strip()

    if command in {"claim", "heartbeat", "block", "release"}:
        if claim_id is None or run_id is None:
            raise ControlError(f"{command} requires claim_id and run_id.")
    if command == "block" and reason is None:
        raise ControlError("block requires a reason.")
    return ControlRequest(command, claim_id, run_id, reason)


def format_control_request(request: ControlRequest) -> str:
    payload = {
        key: value
        for key, value in {
            "command": request.command,
            "claim_id": request.claim_id,
            "run_id": request.run_id,
            "reason": request.reason,
        }.items()
        if value is not None
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"<!-- robodata-agent-control:v1\n{encoded}\n-->"


def format_receipt(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    result = payload.get("result", "recorded")
    command = payload.get("command", "event")
    return (
        f"Agent control `{command}`: **{result}**.\n\n"
        f"<!-- robodata-agent-receipt:v1\n{encoded}\n-->"
    )


def parse_receipts(comment_bodies: Iterable[str]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for body in comment_bodies:
        for match in RECEIPT_RE.finditer(body):
            try:
                payload = json.loads(match.group("payload"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                receipts.append(payload)
    return receipts


def trusted_receipt_bodies(
    comments: Iterable[Mapping[str, Any]],
) -> Iterable[str]:
    for comment in comments:
        author = comment.get("user")
        if (
            isinstance(author, Mapping)
            and author.get("login") == "github-actions[bot]"
            and isinstance(comment.get("body"), str)
        ):
            yield comment["body"]


def matching_claim_receipt(
    receipts: Iterable[Mapping[str, Any]], request: ControlRequest
) -> Mapping[str, Any] | None:
    """Return a replayable pending/accepted receipt or reject ID reuse."""
    assert request.claim_id and request.run_id
    matching = [
        receipt
        for receipt in receipts
        if receipt.get("claim_id") == request.claim_id
        and receipt.get("run_id") == request.run_id
    ]
    if not matching:
        return None
    claim: dict[str, Any] | None = None
    for receipt in matching:
        command = receipt.get("command")
        result = receipt.get("result")
        if command == "claim" and result in {"pending", "accepted"}:
            claim = dict(receipt)
        elif command == "heartbeat" and result == "accepted" and claim is not None:
            claim["lease_until"] = receipt.get("lease_until")
            claim["result"] = "accepted"
        elif command in {"block", "release", "complete"} and result in {
            "accepted",
            "reconciled",
        }:
            raise ControlError(
                "Claim ID has already been finalized and cannot be reused."
            )
        elif command == "claim" and result == "rejected":
            raise ControlError(
                "Claim ID has already been rejected and cannot be reused."
            )
    return claim


def live_receipt_claims(
    receipts: Iterable[Mapping[str, Any]], now: datetime
) -> list[ClaimState]:
    """Reduce receipts to all live claims; more than one is a fail-closed state."""
    states: dict[tuple[str, str], ClaimState] = {}
    for receipt in receipts:
        claim_id = receipt.get("claim_id")
        run_id = receipt.get("run_id")
        if not isinstance(claim_id, str) or not isinstance(run_id, str):
            continue
        key = (claim_id, run_id)
        command = receipt.get("command")
        result = receipt.get("result")
        if command == "claim" and result in {"pending", "accepted"}:
            try:
                states[key] = ClaimState(
                    claim_id,
                    run_id,
                    int(receipt["attempt"]),
                    parse_timestamp(str(receipt["lease_until"])),
                )
            except (ControlError, KeyError, TypeError, ValueError):
                continue
        elif command == "heartbeat" and result == "accepted" and key in states:
            try:
                states[key] = ClaimState(
                    claim_id,
                    run_id,
                    states[key].attempt,
                    parse_timestamp(str(receipt["lease_until"])),
                )
            except (ControlError, KeyError):
                continue
        elif command in {"block", "release", "complete"} and result in {
            "accepted",
            "reconciled",
        }:
            states.pop(key, None)
    return [claim for claim in states.values() if claim.is_live(now)]


def form_sections(body: str) -> dict[str, str]:
    matches = list(FORM_HEADING_RE.finditer(body))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        result[match.group(1).strip()] = body[match.end() : end].strip()
    return result


def issue_kind(body: str, labels: Iterable[str]) -> str | None:
    marker = re.search(r"<!--\s*robodata-kind:(spec|requirement|bug)\s*-->", body)
    if marker:
        return marker.group(1)
    label_set = set(labels)
    if "kind:spec" in label_set:
        return "spec"
    if "kind:requirement" in label_set:
        return "requirement"
    if "bug" in label_set:
        return "bug"
    return None


def _filled(value: str | None) -> bool:
    if value is None:
        return False
    without_comments = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL).strip()
    return without_comments.lower() not in PLACEHOLDERS


def ready_errors(body: str, kind: str | None) -> list[str]:
    errors: list[str] = []
    if kind not in LEAF_KINDS:
        return ["Only Requirement or Bug leaf issues can become Ready."]

    sections = form_sections(body)
    required = (
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
    for name in required:
        if not _filled(sections.get(name)):
            errors.append(f"Missing or empty Ready field: {name}.")

    if parent_spec_number(body) is None:
        errors.append("Requirement/Bug leaf must reference exactly one Parent Spec.")
    size = sections.get("Size", "").strip()
    if size not in {"S", "M"}:
        errors.append("Ready leaf Size must be S or M.")
    profile = sections.get("Validation profile", "").strip()
    if profile not in VALIDATION_PROFILES:
        errors.append("Validation profile is not recognized.")
    return errors


def dependency_numbers(body: str) -> list[int]:
    value = form_sections(body).get("Dependencies", "")
    return sorted({int(number) for number in re.findall(r"(?<!\w)#(\d+)\b", value)})


def parent_spec_number(body: str) -> int | None:
    value = form_sections(body).get("Parent Spec", "")
    numbers = {int(number) for number in re.findall(r"(?<!\w)#(\d+)\b", value)}
    if len(numbers) != 1:
        return None
    return numbers.pop()


def claim_from_fields(values: Mapping[str, Any]) -> ClaimState | None:
    claim_id = values.get("Claim ID")
    run_id = values.get("Agent Run")
    lease = values.get("Lease Until")
    attempt = values.get("Attempt")
    if not any((claim_id, run_id, lease)):
        return None
    if not all((claim_id, run_id, lease)) or attempt is None:
        raise ControlError("Project claim fields are incomplete; refusing takeover.")
    if not isinstance(claim_id, str) or not isinstance(run_id, str):
        raise ControlError("Project claim identifiers are malformed.")
    try:
        parsed_attempt = int(attempt)
    except (TypeError, ValueError) as exc:
        raise ControlError("Project Attempt is malformed.") from exc
    return ClaimState(claim_id, run_id, parsed_attempt, parse_timestamp(str(lease)))


def next_claim(
    *,
    request: ControlRequest,
    values: Mapping[str, Any],
    now: datetime,
    lease_seconds: int,
    max_attempts: int,
) -> tuple[ClaimState, bool]:
    """Return the proposed claim and whether it is an idempotent replay."""
    assert request.claim_id and request.run_id
    current = claim_from_fields(values)
    status = values.get("Status")
    if current and current.claim_id == request.claim_id:
        if current.run_id != request.run_id:
            raise ControlError("Claim ID already belongs to a different Agent Run.")
        return current, True
    if status not in {"Ready", "Recovery"}:
        raise ControlError(f"Issue is not claimable from Status={status!r}.")
    if current and current.is_live(now):
        raise ControlError(
            f"Another claim is live until {isoformat_utc(current.lease_until)}."
        )
    try:
        previous_attempt = int(values.get("Attempt") or 0)
    except (TypeError, ValueError) as exc:
        raise ControlError("Project Attempt is malformed.") from exc
    attempt = previous_attempt + 1
    if attempt > max_attempts:
        raise ControlError(f"Attempt limit ({max_attempts}) has been reached.")
    return (
        ClaimState(
            claim_id=request.claim_id,
            run_id=request.run_id,
            attempt=attempt,
            lease_until=now + timedelta(seconds=lease_seconds),
        ),
        False,
    )


def slugify(title: str, fallback: str = "work") -> str:
    value = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (value[:48].rstrip("-") or fallback)


class GitHubClient:
    def __init__(self, *, repo_token: str, project_token: str) -> None:
        self.repo_token = repo_token
        self.project_token = project_token

    @staticmethod
    def _safe_error(body: bytes) -> str:
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(payload, dict) and isinstance(payload.get("message"), str):
                return payload["message"][:500]
        except json.JSONDecodeError:
            pass
        return "GitHub API request failed"

    def _redact(self, value: str) -> str:
        return value.replace(self.repo_token, "[REDACTED]").replace(
            self.project_token, "[REDACTED]"
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        data = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "robodata-agent-control/1",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            raise ControlError(
                f"GitHub API returned HTTP {exc.code}: "
                f"{self._redact(self._safe_error(body))}"
            ) from None
        except urllib.error.URLError as exc:
            raise ControlError(
                f"GitHub API connection failed: {self._redact(str(exc.reason))}"
            ) from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ControlError("GitHub API returned invalid JSON.") from exc

    def rest(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        return self.request(
            method,
            f"https://api.github.com{path}",
            token=self.repo_token,
            payload=payload,
        )

    def graphql(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        result = self.request(
            "POST",
            "https://api.github.com/graphql",
            token=self.project_token,
            payload={"query": query, "variables": dict(variables)},
        )
        if not isinstance(result, dict):
            raise ControlError("GitHub GraphQL response is not an object.")
        errors = result.get("errors")
        if errors:
            messages = [
                self._redact(item.get("message", "unknown GraphQL error"))
                for item in errors
                if isinstance(item, dict)
                and isinstance(item.get("message", "unknown GraphQL error"), str)
            ]
            raise ControlError("GitHub GraphQL error: " + "; ".join(messages)[:500])
        data = result.get("data")
        if not isinstance(data, dict):
            raise ControlError("GitHub GraphQL response has no data.")
        return data

    def list_comments(self, repository: str, issue_number: int) -> list[dict[str, Any]]:
        owner, repo = repository.split("/", 1)
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.rest(
                "GET",
                f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
                f"?per_page=100&page={page}",
            )
            if not isinstance(batch, list):
                raise ControlError("Issue comments response is malformed.")
            comments.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                return comments
            page += 1

    def post_comment(self, repository: str, issue_number: int, body: str) -> None:
        owner, repo = repository.split("/", 1)
        self.rest(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            {"body": body},
        )

    def get_issue(self, repository: str, issue_number: int) -> dict[str, Any]:
        owner, repo = repository.split("/", 1)
        issue = self.rest("GET", f"/repos/{owner}/{repo}/issues/{issue_number}")
        if not isinstance(issue, dict):
            raise ControlError(f"Issue #{issue_number} response is malformed.")
        return issue

    def get_pull(self, repository: str, pull_number: int) -> dict[str, Any]:
        owner, repo = repository.split("/", 1)
        pull = self.rest("GET", f"/repos/{owner}/{repo}/pulls/{pull_number}")
        if not isinstance(pull, dict):
            raise ControlError(f"Pull request #{pull_number} response is malformed.")
        return pull

    def remove_label(self, repository: str, issue_number: int, label: str) -> None:
        owner, repo = repository.split("/", 1)
        encoded = urllib.parse.quote(label, safe="")
        try:
            self.rest(
                "DELETE",
                f"/repos/{owner}/{repo}/issues/{issue_number}/labels/{encoded}",
            )
        except ControlError as exc:
            if "HTTP 404" not in str(exc):
                raise

    def dependency_errors(self, repository: str, body: str) -> list[str]:
        errors: list[str] = []
        for number in dependency_numbers(body):
            issue = self.get_issue(repository, number)
            if issue.get("state") != "closed":
                errors.append(f"Dependency #{number} is still open.")
        return errors

    def parent_spec_errors(
        self, repository: str, issue_number: int, body: str
    ) -> list[str]:
        parent_number = parent_spec_number(body)
        if parent_number is None:
            return []
        if parent_number == issue_number:
            return ["Parent Spec cannot be the leaf issue itself."]
        parent = self.get_issue(repository, parent_number)
        labels = {
            label.get("name")
            for label in parent.get("labels", [])
            if isinstance(label, dict)
        }
        errors: list[str] = []
        if "kind:spec" not in labels:
            errors.append(f"Parent #{parent_number} is not labeled kind:spec.")
        if parent.get("state") != "open":
            errors.append(f"Parent Spec #{parent_number} is closed.")
        return errors

    def has_open_pr(self, repository: str, issue_number: int) -> bool:
        owner, repo = repository.split("/", 1)
        page = 1
        while True:
            pulls = self.rest(
                "GET",
                f"/repos/{owner}/{repo}/pulls?state=open&per_page=100&page={page}",
            )
            if not isinstance(pulls, list):
                raise ControlError("Open pull request response is malformed.")
            for pull in pulls:
                if not isinstance(pull, dict):
                    continue
                numbers = {
                    int(match.group("number"))
                    for match in CLOSES_RE.finditer(pull.get("body") or "")
                }
                if issue_number in numbers:
                    return True
            if len(pulls) < 100:
                return False
            page += 1


PROJECT_QUERY = """
query Project($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      title
      fields(first: 100) {
        nodes {
          __typename
          ... on ProjectV2Field { id name dataType }
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name }
          }
        }
      }
    }
  }
}
"""
ADD_ITEM_MUTATION = """
mutation AddItem($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
  }
}
"""
ITEM_VALUES_QUERY = """
query ItemValues($item: ID!) {
  node(id: $item) {
    ... on ProjectV2Item {
      fieldValues(first: 100) {
        nodes {
          __typename
          ... on ProjectV2ItemFieldTextValue {
            text
            field { ... on ProjectV2Field { id name } }
          }
          ... on ProjectV2ItemFieldNumberValue {
            number
            field { ... on ProjectV2Field { id name } }
          }
          ... on ProjectV2ItemFieldSingleSelectValue {
            name
            optionId
            field { ... on ProjectV2SingleSelectField { id name } }
          }
        }
      }
    }
  }
}
"""
SET_FIELD_MUTATION = """
mutation SetField(
  $project: ID!,
  $item: ID!,
  $field: ID!,
  $value: ProjectV2FieldValue!
) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project,
    itemId: $item,
    fieldId: $field,
    value: $value
  }) {
    projectV2Item { id }
  }
}
"""
CLEAR_FIELD_MUTATION = """
mutation ClearField($project: ID!, $item: ID!, $field: ID!) {
  clearProjectV2ItemFieldValue(input: {
    projectId: $project,
    itemId: $item,
    fieldId: $field
  }) {
    projectV2Item { id }
  }
}
"""
PROJECT_ITEMS_QUERY = """
query ProjectItems($owner: String!, $number: Int!, $after: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $after) {
        nodes {
          id
          content {
            ... on Issue {
              id
              number
              repository { nameWithOwner }
            }
          }
          fieldValues(first: 100) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldTextValue {
                text
                field { ... on ProjectV2Field { id name } }
              }
              ... on ProjectV2ItemFieldNumberValue {
                number
                field { ... on ProjectV2Field { id name } }
              }
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                optionId
                field { ... on ProjectV2SingleSelectField { id name } }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def _field_values(nodes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for node in nodes:
        field = node.get("field")
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            continue
        name = field["name"]
        typename = node.get("__typename")
        if typename == "ProjectV2ItemFieldSingleSelectValue":
            values[name] = node.get("name")
        elif typename == "ProjectV2ItemFieldTextValue":
            values[name] = node.get("text")
        elif typename == "ProjectV2ItemFieldNumberValue":
            values[name] = node.get("number")
    return values


class ProjectGateway:
    def __init__(self, client: GitHubClient, owner: str, number: int) -> None:
        self.client = client
        self.owner = owner
        self.number = number
        data = client.graphql(PROJECT_QUERY, {"owner": owner, "number": number})
        project = (data.get("user") or {}).get("projectV2")
        if not isinstance(project, dict):
            raise ControlError(f"Project #{number} was not found for {owner}.")
        self.project_id = project["id"]
        self.fields: dict[str, dict[str, Any]] = {}
        for field in project.get("fields", {}).get("nodes", []):
            if isinstance(field, dict) and isinstance(field.get("name"), str):
                self.fields[field["name"]] = field

    def ensure_issue(self, issue_node_id: str) -> tuple[str, dict[str, Any]]:
        data = self.client.graphql(
            ADD_ITEM_MUTATION,
            {"project": self.project_id, "content": issue_node_id},
        )
        item_id = data["addProjectV2ItemById"]["item"]["id"]
        return item_id, self.get_values(item_id)

    def get_values(self, item_id: str) -> dict[str, Any]:
        data = self.client.graphql(ITEM_VALUES_QUERY, {"item": item_id})
        node = data.get("node")
        if not isinstance(node, dict):
            raise ControlError("Project item disappeared while reading fields.")
        nodes = node.get("fieldValues", {}).get("nodes", [])
        return _field_values(nodes)

    def set_fields(self, item_id: str, updates: Mapping[str, Any]) -> None:
        for name, value in updates.items():
            field = self.fields.get(name)
            if field is None:
                raise ControlError(f"Project field is missing: {name}.")
            if value is None or value == "":
                self.client.graphql(
                    CLEAR_FIELD_MUTATION,
                    {
                        "project": self.project_id,
                        "item": item_id,
                        "field": field["id"],
                    },
                )
                continue
            if name in PROJECT_SINGLE_SELECTS:
                options = {
                    option["name"]: option["id"]
                    for option in field.get("options", [])
                    if isinstance(option, dict)
                }
                if value not in options:
                    raise ControlError(f"Project option is missing: {name}={value}.")
                encoded = {"singleSelectOptionId": options[value]}
            elif name == "Attempt":
                encoded = {"number": float(value)}
            else:
                encoded = {"text": str(value)}
            self.client.graphql(
                SET_FIELD_MUTATION,
                {
                    "project": self.project_id,
                    "item": item_id,
                    "field": field["id"],
                    "value": encoded,
                },
            )

    def list_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = self.client.graphql(
                PROJECT_ITEMS_QUERY,
                {"owner": self.owner, "number": self.number, "after": after},
            )
            connection = data["user"]["projectV2"]["items"]
            for node in connection["nodes"]:
                if not isinstance(node, dict):
                    continue
                copied = dict(node)
                copied["values"] = _field_values(
                    node.get("fieldValues", {}).get("nodes", [])
                )
                items.append(copied)
            page = connection["pageInfo"]
            if not page["hasNextPage"]:
                return items
            after = page["endCursor"]


def _labels(issue: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for label in issue.get("labels", []):
        if isinstance(label, str):
            result.append(label)
        elif isinstance(label, dict) and isinstance(label.get("name"), str):
            result.append(label["name"])
    return result


def _project_dimensions(body: str, kind: str | None) -> dict[str, Any]:
    sections = form_sections(body)
    kind_name = {"spec": "Spec", "requirement": "Requirement", "bug": "Bug"}.get(kind)
    updates: dict[str, Any] = {}
    if kind_name:
        updates["Type"] = kind_name
    for section, field in (
        ("Priority", "Priority"),
        ("Area", "Area"),
        ("Risk", "Risk"),
        ("Size", "Size"),
        ("Validation profile", "Validation Profile"),
    ):
        value = sections.get(section, "").strip()
        if value:
            updates[field] = value
    return updates


class AgentController:
    def __init__(
        self,
        *,
        client: GitHubClient,
        project: ProjectGateway,
        repository: str,
        repository_owner: str,
        lease_seconds: int = 7_200,
        max_attempts: int = 3,
    ) -> None:
        self.client = client
        self.project = project
        self.repository = repository
        self.repository_owner = repository_owner
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def sync_issue(self, issue: Mapping[str, Any], action: str) -> None:
        item_id, values = self.project.ensure_issue(str(issue["node_id"]))
        kind = issue_kind(issue.get("body") or "", _labels(issue))
        updates = _project_dimensions(issue.get("body") or "", kind)
        if action == "closed":
            comments = self.client.list_comments(
                self.repository, int(issue["number"])
            )
            for claim in live_receipt_claims(
                parse_receipts(trusted_receipt_bodies(comments)), utc_now()
            ):
                self._receipt(
                    int(issue["number"]),
                    ControlRequest(
                        "complete",
                        claim_id=claim.claim_id,
                        run_id=claim.run_id,
                    ),
                    result="accepted",
                    reason="Issue closed.",
                    attempt=claim.attempt,
                    status="Done",
                )
            updates["Status"] = "Done"
            updates["Claim ID"] = None
            updates["Agent Run"] = None
            updates["Lease Until"] = None
        elif action == "reopened" and values.get("Status") in {None, "Done"}:
            updates["Status"] = "Inbox"
        elif not values.get("Status"):
            updates["Status"] = "Inbox"
        self.project.set_fields(item_id, updates)

    def _receipt(
        self,
        issue_number: int,
        request: ControlRequest,
        *,
        result: str,
        **extra: Any,
    ) -> None:
        payload = {
            "version": 1,
            "command": request.command,
            "result": result,
            "claim_id": request.claim_id,
            "run_id": request.run_id,
            **extra,
        }
        self.client.post_comment(
            self.repository, issue_number, format_receipt(payload)
        )

    def _ready_failures(
        self, issue: Mapping[str, Any], *, check_open_pr: bool = True
    ) -> list[str]:
        body = issue.get("body") or ""
        failures = ready_errors(body, issue_kind(body, _labels(issue)))
        if issue.get("state") != "open":
            failures.append("The leaf issue is closed.")
        failures.extend(
            self.client.parent_spec_errors(
                self.repository, int(issue["number"]), body
            )
        )
        failures.extend(self.client.dependency_errors(self.repository, body))
        if check_open_pr and self.client.has_open_pr(
            self.repository, int(issue["number"])
        ):
            failures.append("The leaf already has an open pull request.")
        return failures

    def handle_request(
        self,
        *,
        issue: Mapping[str, Any],
        request: ControlRequest,
        now: datetime,
    ) -> None:
        issue_number = int(issue["number"])
        item_id, values = self.project.ensure_issue(str(issue["node_id"]))
        kind = issue_kind(issue.get("body") or "", _labels(issue))
        dimensions = _project_dimensions(issue.get("body") or "", kind)
        if dimensions:
            self.project.set_fields(item_id, dimensions)
            values.update(dimensions)

        if request.command == "ready":
            try:
                current = claim_from_fields(values)
            except ControlError as exc:
                self.project.set_fields(item_id, {"Status": "Blocked"})
                self._receipt(
                    issue_number,
                    request,
                    result="rejected",
                    reason=str(exc),
                    status="Blocked",
                )
                return
            if values.get("Status") in LEASE_STATUSES or (
                current is not None and current.is_live(now)
            ):
                self._receipt(
                    issue_number,
                    request,
                    result="rejected",
                    reason="A live claim cannot be moved back to Ready.",
                    status=values.get("Status"),
                )
                return
            failures = self._ready_failures(issue)
            if failures:
                self.project.set_fields(item_id, {"Status": "Needs spec"})
                self._receipt(
                    issue_number,
                    request,
                    result="rejected",
                    reason=" ".join(failures),
                    status="Needs spec",
                )
                return
            self.project.set_fields(
                item_id,
                {
                    "Status": "Ready",
                    "Claim ID": None,
                    "Agent Run": None,
                    "Lease Until": None,
                },
            )
            self.client.remove_label(self.repository, issue_number, "needs-triage")
            self._receipt(
                issue_number, request, result="accepted", status="Ready"
            )
            return

        if request.command == "claim":
            failures = self._ready_failures(issue)
            if failures:
                self._receipt(
                    issue_number,
                    request,
                    result="rejected",
                    reason=" ".join(failures),
                )
                return
            comments = self.client.list_comments(self.repository, issue_number)
            receipts = parse_receipts(trusted_receipt_bodies(comments))
            try:
                live_audit = live_receipt_claims(receipts, now)
                conflicting = [
                    claim
                    for claim in live_audit
                    if claim.claim_id != request.claim_id
                    or claim.run_id != request.run_id
                ]
                if conflicting:
                    raise ControlError(
                        "Another accepted or pending claim is still live."
                    )
                existing = matching_claim_receipt(receipts, request)
                if existing is not None:
                    claim = ClaimState(
                        claim_id=str(existing["claim_id"]),
                        run_id=str(existing["run_id"]),
                        attempt=int(existing["attempt"]),
                        lease_until=parse_timestamp(str(existing["lease_until"])),
                    )
                    replay = True
                else:
                    field_claim = claim_from_fields(values)
                    if field_claim is not None and not field_claim.is_live(now):
                        self.project.set_fields(
                            item_id,
                            {
                                "Status": "Recovery",
                                "Claim ID": None,
                                "Agent Run": None,
                                "Lease Until": None,
                            },
                        )
                        values = {
                            **values,
                            "Status": "Recovery",
                            "Claim ID": None,
                            "Agent Run": None,
                            "Lease Until": None,
                        }
                    receipt_attempts = [
                        int(receipt["attempt"])
                        for receipt in receipts
                        if receipt.get("command") == "claim"
                        and receipt.get("result") in {"pending", "accepted"}
                        and isinstance(receipt.get("attempt"), (int, float))
                    ]
                    claim_values = dict(values)
                    claim_values["Attempt"] = max(
                        [int(values.get("Attempt") or 0), *receipt_attempts]
                    )
                    claim, replay = next_claim(
                        request=request,
                        values=claim_values,
                        now=now,
                        lease_seconds=self.lease_seconds,
                        max_attempts=self.max_attempts,
                    )
            except ControlError as exc:
                if "Attempt limit" in str(exc):
                    self.project.set_fields(item_id, {"Status": "Blocked"})
                self._receipt(
                    issue_number,
                    request,
                    result="rejected",
                    reason=str(exc),
                )
                return
            if not claim.is_live(now):
                self.project.set_fields(
                    item_id,
                    {
                        "Status": "Recovery",
                        "Claim ID": None,
                        "Agent Run": None,
                        "Lease Until": None,
                    },
                )
                self._receipt(
                    issue_number,
                    request,
                    result="rejected",
                    reason="Recorded claim lease has expired; use a new Claim ID.",
                    status="Recovery",
                )
                return
            branch = (
                f"agent/{issue_number}-a{claim.attempt}-"
                f"{slugify(str(issue.get('title') or 'work'))}"
            )
            if existing is None:
                self._receipt(
                    issue_number,
                    request,
                    result="pending",
                    attempt=claim.attempt,
                    lease_until=isoformat_utc(claim.lease_until),
                    branch=branch,
                    status="In progress",
                )
            self.project.set_fields(
                item_id,
                {
                    "Status": "In progress",
                    "Claim ID": claim.claim_id,
                    "Agent Run": claim.run_id,
                    "Lease Until": isoformat_utc(claim.lease_until),
                    "Attempt": claim.attempt,
                },
            )
            if existing is None or existing.get("result") != "accepted":
                self._receipt(
                    issue_number,
                    request,
                    result="accepted",
                    attempt=claim.attempt,
                    lease_until=isoformat_utc(claim.lease_until),
                    branch=branch,
                    replay=replay,
                    status="In progress",
                )
            return

        try:
            current = claim_from_fields(values)
        except ControlError as exc:
            self.project.set_fields(item_id, {"Status": "Blocked"})
            self._receipt(
                issue_number,
                request,
                result="rejected",
                reason=str(exc),
                status="Blocked",
            )
            return
        if (
            current is None
            or current.claim_id != request.claim_id
            or current.run_id != request.run_id
        ):
            self._receipt(
                issue_number,
                request,
                result="rejected",
                reason="Claim ID or Agent Run does not match the active claim.",
            )
            return
        if not current.is_live(now):
            self.project.set_fields(
                item_id,
                {
                    "Status": "Recovery",
                    "Claim ID": None,
                    "Agent Run": None,
                    "Lease Until": None,
                },
            )
            self._receipt(
                issue_number,
                request,
                result="rejected",
                reason="Lease has expired; a new attempt is required.",
                status="Recovery",
            )
            return

        if request.command == "heartbeat":
            extended = now + timedelta(seconds=self.lease_seconds)
            self.project.set_fields(
                item_id, {"Lease Until": isoformat_utc(extended)}
            )
            self._receipt(
                issue_number,
                request,
                result="accepted",
                attempt=current.attempt,
                lease_until=isoformat_utc(extended),
                status=values.get("Status"),
            )
        elif request.command == "block":
            self.project.set_fields(
                item_id,
                {
                    "Status": "Blocked",
                    "Claim ID": None,
                    "Agent Run": None,
                    "Lease Until": None,
                },
            )
            self._receipt(
                issue_number,
                request,
                result="accepted",
                reason=request.reason,
                status="Blocked",
            )
        elif request.command == "release":
            self.project.set_fields(
                item_id,
                {
                    "Status": "Recovery",
                    "Claim ID": None,
                    "Agent Run": None,
                    "Lease Until": None,
                },
            )
            self._receipt(
                issue_number,
                request,
                result="accepted",
                reason=request.reason,
                status="Recovery",
            )

    def sync_pull_request(self, pull_request: Mapping[str, Any], action: str) -> None:
        body = pull_request.get("body") or ""
        closing = list(CLOSES_RE.finditer(body))
        if len(closing) != 1:
            return
        issue_number = int(closing[0].group("number"))
        issue = self.client.get_issue(self.repository, issue_number)
        item_id, values = self.project.ensure_issue(str(issue["node_id"]))
        claim_id_match = CLAIM_ID_RE.search(body)
        run_id_match = AGENT_RUN_RE.search(body)
        if not claim_id_match or not run_id_match:
            return
        if (
            values.get("Claim ID") != claim_id_match.group("value")
            or values.get("Agent Run") != run_id_match.group("value")
        ):
            return

        if action == "closed":
            merged = bool(pull_request.get("merged"))
            status = "Done" if merged else "Recovery"
            self.project.set_fields(
                item_id,
                {
                    "Status": status,
                    "Claim ID": None,
                    "Agent Run": None,
                    "Lease Until": None,
                },
            )
            request = ControlRequest(
                "complete" if merged else "release",
                claim_id=claim_id_match.group("value"),
                run_id=run_id_match.group("value"),
            )
            self._receipt(
                issue_number,
                request,
                result="accepted",
                reason="Pull request merged." if merged else "Pull request closed.",
                status=status,
            )
        else:
            try:
                current = claim_from_fields(values)
            except ControlError:
                return
            now = utc_now()
            if current is None or not current.is_live(now):
                self.project.set_fields(
                    item_id,
                    {
                        "Status": "Recovery",
                        "Claim ID": None,
                        "Agent Run": None,
                        "Lease Until": None,
                    },
                )
                return
            status = "In progress" if pull_request.get("draft") else "In review"
            extended = now + timedelta(seconds=self.lease_seconds)
            self.project.set_fields(
                item_id,
                {
                    "Status": status,
                    "Lease Until": isoformat_utc(extended),
                },
            )
            self._receipt(
                issue_number,
                ControlRequest(
                    "heartbeat",
                    claim_id=current.claim_id,
                    run_id=current.run_id,
                ),
                result="accepted",
                attempt=current.attempt,
                lease_until=isoformat_utc(extended),
                status=status,
                source=f"pull_request:{action}",
            )

    def reconcile(self, now: datetime) -> int:
        repaired = 0
        for item in self.project.list_items():
            content = item.get("content")
            values = item.get("values", {})
            if (
                not isinstance(content, dict)
                or content.get("repository", {}).get("nameWithOwner")
                != self.repository
                or values.get("Status") not in LEASE_STATUSES
            ):
                continue
            try:
                current = claim_from_fields(values)
            except ControlError as exc:
                self.project.set_fields(
                    item["id"],
                    {
                        "Status": "Blocked",
                        "Claim ID": None,
                        "Agent Run": None,
                        "Lease Until": None,
                    },
                )
                request = ControlRequest("release")
                self._receipt(
                    int(content["number"]),
                    request,
                    result="reconciled",
                    reason=str(exc),
                    status="Blocked",
                )
                repaired += 1
                continue
            if current is None or current.is_live(now):
                continue
            status = (
                "Blocked" if current.attempt >= self.max_attempts else "Recovery"
            )
            self.project.set_fields(
                item["id"],
                {
                    "Status": status,
                    "Claim ID": None,
                    "Agent Run": None,
                    "Lease Until": None,
                },
            )
            request = ControlRequest(
                "release", claim_id=current.claim_id, run_id=current.run_id
            )
            self._receipt(
                int(content["number"]),
                request,
                result="reconciled",
                reason="Lease expired.",
                attempt=current.attempt,
                status=status,
            )
            repaired += 1
        return repaired


def _read_event(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError(f"Cannot read GitHub event JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlError("GitHub event root must be an object.")
    return value


def run_event(event: Mapping[str, Any], event_name: str) -> int:
    repo_token = os.environ.get("GITHUB_TOKEN", "")
    project_token = os.environ.get("PROJECTS_CLASSIC_PAT", "")
    owner = os.environ.get("PROJECT_OWNER", "")
    project_number = os.environ.get("PROJECT_NUMBER", "")
    if not repo_token or not project_token:
        raise ControlError("GITHUB_TOKEN and PROJECTS_CLASSIC_PAT are required.")
    if not owner or not project_number.isdigit():
        raise ControlError("PROJECT_OWNER and numeric PROJECT_NUMBER are required.")
    repository = event.get("repository", {}).get("full_name") or os.environ.get(
        "GITHUB_REPOSITORY", ""
    )
    repository_owner = event.get("repository", {}).get("owner", {}).get(
        "login"
    ) or repository.partition("/")[0]
    if not repository or owner != repository_owner:
        raise ControlError("Project owner must match the repository owner.")

    client = GitHubClient(repo_token=repo_token, project_token=project_token)
    project = ProjectGateway(client, owner, int(project_number))
    controller = AgentController(
        client=client,
        project=project,
        repository=repository,
        repository_owner=repository_owner,
        lease_seconds=int(os.environ.get("AGENT_LEASE_SECONDS", "7200")),
        max_attempts=int(os.environ.get("AGENT_MAX_ATTEMPTS", "3")),
    )
    action = str(event.get("action") or "")

    if event_name == "issues":
        current_issue = client.get_issue(repository, int(event["issue"]["number"]))
        effective_action = (
            "closed"
            if current_issue.get("state") == "closed"
            else ("edited" if action == "closed" else action)
        )
        controller.sync_issue(current_issue, effective_action)
    elif event_name == "issue_comment":
        if "pull_request" in event.get("issue", {}):
            return 0
        if event.get("comment", {}).get("user", {}).get("login") != repository_owner:
            return 0
        request = parse_control_request(event.get("comment", {}).get("body") or "")
        if request is None:
            return 0
        current_issue = client.get_issue(repository, int(event["issue"]["number"]))
        controller.handle_request(
            issue=current_issue, request=request, now=utc_now()
        )
    elif event_name == "pull_request":
        current_pull = client.get_pull(
            repository, int(event["pull_request"]["number"])
        )
        effective_action = (
            "closed" if current_pull.get("state") == "closed" else "current"
        )
        controller.sync_pull_request(current_pull, effective_action)
    elif event_name in {"schedule", "workflow_dispatch"}:
        repaired = controller.reconcile(utc_now())
        print(f"reconciled={repaired}")
    else:
        raise ControlError(f"Unsupported GitHub event: {event_name}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--event",
        type=Path,
        default=Path(os.environ.get("GITHUB_EVENT_PATH", "")),
    )
    parser.add_argument(
        "--event-name", default=os.environ.get("GITHUB_EVENT_NAME", "")
    )
    args = parser.parse_args(argv)
    if not str(args.event) or not args.event_name:
        print("--event and --event-name (or GitHub equivalents) are required.", file=sys.stderr)
        return 2
    try:
        return run_event(_read_event(args.event), args.event_name)
    except (ControlError, KeyError, TypeError, ValueError) as exc:
        print(f"agent-control: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
