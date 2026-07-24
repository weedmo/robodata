#!/usr/bin/env python3
"""Reconcile the repository's name-based GitHub Project v2 manifest.

The script is read-only unless ``--apply`` is supplied. It intentionally never
deletes projects, fields, options, or views.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


GRAPHQL_ENDPOINT = "https://api.github.com/graphql"
TOKEN_ENV = "PROJECTS_CLASSIC_PAT"
DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / ".github" / "project.json"
FIELD_TYPES = {
    "single_select": "SINGLE_SELECT",
    "text": "TEXT",
    "number": "NUMBER",
}
OPTION_COLORS = {
    "BLUE",
    "GRAY",
    "GREEN",
    "ORANGE",
    "PINK",
    "PURPLE",
    "RED",
    "YELLOW",
}


class BootstrapError(RuntimeError):
    """A safe-to-print bootstrap failure."""


class GraphQLClient:
    """Small stdlib-only GitHub GraphQL client."""

    def __init__(
        self,
        token: str,
        endpoint: str = GRAPHQL_ENDPOINT,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not token:
            raise BootstrapError(f"{TOKEN_ENV} is not set")
        self._token = token
        self._endpoint = endpoint
        self._opener = opener

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode()
        request = urllib.request.Request(
            self._endpoint,
            data=payload,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "robodata-project-bootstrap",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=30) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise BootstrapError(f"GitHub GraphQL HTTP error: {exc.code}") from None
        except urllib.error.URLError as exc:
            reason = str(exc.reason).replace(self._token, "[REDACTED]")
            raise BootstrapError(f"GitHub GraphQL connection failed: {reason}") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BootstrapError("GitHub GraphQL returned invalid JSON") from None

        if not isinstance(result, dict):
            raise BootstrapError("GitHub GraphQL returned an invalid response")
        errors = result.get("errors")
        if errors:
            messages = [
                str(error.get("message", "unknown GraphQL error")).replace(
                    self._token, "[REDACTED]"
                )
                for error in errors
                if isinstance(error, dict)
            ]
            if not messages:
                messages = ["unknown GraphQL error"]
            raise BootstrapError("GitHub GraphQL error: " + "; ".join(messages))
        data = result.get("data")
        if not isinstance(data, dict):
            raise BootstrapError("GitHub GraphQL response has no data")
        return data


OWNER_PROJECTS_QUERY = """
query OwnerProjects($login: String!, $after: String) {
  user(login: $login) {
    id
    projectsV2(first: 100, after: $after) {
      nodes { id title number }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PROJECT_FIELDS_QUERY = """
query ProjectFields($projectId: ID!, $after: String) {
  node(id: $projectId) {
    ... on ProjectV2 {
      fields(first: 100, after: $after) {
        nodes {
          ... on ProjectV2Field { id name dataType }
          ... on ProjectV2SingleSelectField {
            id
            name
            dataType
            options { id name color description }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

CREATE_PROJECT_MUTATION = """
mutation CreateProject($ownerId: ID!, $title: String!) {
  createProjectV2(input: {ownerId: $ownerId, title: $title}) {
    projectV2 { id title number }
  }
}
"""

CREATE_FIELD_MUTATION = """
mutation CreateField(
  $projectId: ID!,
  $name: String!,
  $dataType: ProjectV2CustomFieldType!,
  $options: [ProjectV2SingleSelectFieldOptionInput!]
) {
  createProjectV2Field(input: {
    projectId: $projectId,
    name: $name,
    dataType: $dataType,
    singleSelectOptions: $options
  }) {
    projectV2Field {
      ... on ProjectV2Field { id name dataType }
      ... on ProjectV2SingleSelectField { id name dataType }
    }
  }
}
"""

UPDATE_SELECT_FIELD_MUTATION = """
mutation UpdateSelectField(
  $fieldId: ID!,
  $options: [ProjectV2SingleSelectFieldOptionInput!]!
) {
  updateProjectV2Field(input: {
    fieldId: $fieldId,
    singleSelectOptions: $options
  }) {
    projectV2Field {
      ... on ProjectV2SingleSelectField { id name dataType }
    }
  }
}
"""


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BootstrapError(f"manifest not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"invalid manifest JSON: {exc}") from None
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise BootstrapError("manifest must be a JSON object")
    if manifest.get("version") != 1:
        raise BootstrapError("manifest version must be 1")
    for key in ("owner", "title"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise BootstrapError(f"manifest {key} must be a non-empty string")
    fields = manifest.get("fields")
    if not isinstance(fields, list) or not fields:
        raise BootstrapError("manifest fields must be a non-empty array")

    field_names: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            raise BootstrapError("each field must be an object")
        name = field.get("name")
        field_type = field.get("type")
        if not isinstance(name, str) or not name.strip():
            raise BootstrapError("each field needs a non-empty name")
        if name in field_names:
            raise BootstrapError(f"duplicate field name: {name}")
        field_names.add(name)
        if field_type not in FIELD_TYPES:
            raise BootstrapError(f"unsupported field type for {name}: {field_type}")
        options = field.get("options")
        if field_type == "single_select":
            _validate_options(name, options)
        elif options is not None:
            raise BootstrapError(f"non-select field {name} cannot have options")

    views = manifest.get("views", [])
    if not isinstance(views, list):
        raise BootstrapError("manifest views must be an array")


def _validate_options(field_name: str, options: Any) -> None:
    if not isinstance(options, list) or not options:
        raise BootstrapError(f"single-select field {field_name} needs options")
    names: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            raise BootstrapError(f"option in {field_name} must be an object")
        name = option.get("name")
        color = option.get("color")
        description = option.get("description")
        if not isinstance(name, str) or not name.strip():
            raise BootstrapError(f"option in {field_name} needs a non-empty name")
        if name in names:
            raise BootstrapError(f"duplicate option {name} in field {field_name}")
        names.add(name)
        if color not in OPTION_COLORS:
            raise BootstrapError(f"invalid color for {field_name}/{name}: {color}")
        if not isinstance(description, str):
            raise BootstrapError(f"description for {field_name}/{name} must be a string")


def _connection_pages(
    client: GraphQLClient,
    query: str,
    variables: dict[str, Any],
    connection: Callable[[dict[str, Any]], dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        data = client.execute(query, {**variables, "after": after})
        page = connection(data)
        nodes.extend(node for node in page.get("nodes", []) if isinstance(node, dict))
        page_info = page.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            return nodes
        after = page_info.get("endCursor")
        if not after:
            raise BootstrapError("GitHub pagination omitted endCursor")


def get_owner_and_projects(
    client: GraphQLClient, login: str
) -> tuple[str, list[dict[str, Any]]]:
    owner_id: str | None = None

    def connection(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal owner_id
        user = data.get("user")
        if not isinstance(user, dict):
            raise BootstrapError(f"GitHub user not found: {login}")
        owner_id = user.get("id")
        projects = user.get("projectsV2")
        if not isinstance(projects, dict):
            raise BootstrapError("GitHub user response has no projects connection")
        return projects

    projects = _connection_pages(
        client, OWNER_PROJECTS_QUERY, {"login": login}, connection
    )
    if not owner_id:
        raise BootstrapError(f"GitHub user has no node ID: {login}")
    return owner_id, projects


def get_project_fields(
    client: GraphQLClient, project_id: str
) -> list[dict[str, Any]]:
    def connection(data: dict[str, Any]) -> dict[str, Any]:
        project = data.get("node")
        if not isinstance(project, dict):
            raise BootstrapError("GitHub project was not found")
        fields = project.get("fields")
        if not isinstance(fields, dict):
            raise BootstrapError("GitHub project response has no fields connection")
        return fields

    return _connection_pages(
        client, PROJECT_FIELDS_QUERY, {"projectId": project_id}, connection
    )


def _create_project(
    client: GraphQLClient, owner_id: str, title: str
) -> dict[str, Any]:
    data = client.execute(
        CREATE_PROJECT_MUTATION, {"ownerId": owner_id, "title": title}
    )
    payload = data.get("createProjectV2")
    project = payload.get("projectV2") if isinstance(payload, dict) else None
    if not isinstance(project, dict) or not project.get("id"):
        raise BootstrapError("GitHub did not return the created project")
    return project


def _create_field(
    client: GraphQLClient, project_id: str, field: dict[str, Any]
) -> None:
    data = client.execute(
        CREATE_FIELD_MUTATION,
        {
            "projectId": project_id,
            "name": field["name"],
            "dataType": FIELD_TYPES[field["type"]],
            "options": field.get("options"),
        },
    )
    payload = data.get("createProjectV2Field")
    created = payload.get("projectV2Field") if isinstance(payload, dict) else None
    if not isinstance(created, dict) or not created.get("id"):
        raise BootstrapError(f"GitHub did not return created field {field['name']!r}")


def _merged_options(
    desired: list[dict[str, str]], existing: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Apply desired metadata/order while retaining names and option identities."""
    desired_names = {option["name"] for option in desired}
    existing_by_name = {option["name"]: option for option in existing}
    configured: list[dict[str, str]] = []
    for option in desired:
        merged = dict(option)
        existing_option = existing_by_name.get(option["name"])
        if existing_option and existing_option.get("id"):
            merged["id"] = existing_option["id"]
        configured.append(merged)
    extras = [
        {
            **({"id": option["id"]} if option.get("id") else {}),
            "name": option["name"],
            "color": option["color"],
            "description": option.get("description") or "",
        }
        for option in existing
        if option.get("name") not in desired_names
    ]
    return configured + extras


def _options_changed(
    desired: list[dict[str, str]], existing: list[dict[str, str]]
) -> bool:
    normalized_existing = [
        {
            "name": option.get("name"),
            "color": option.get("color"),
            "description": option.get("description") or "",
        }
        for option in existing
    ]
    normalized_target = [
        {key: value for key, value in option.items() if key != "id"}
        for option in _merged_options(desired, existing)
    ]
    return normalized_target != normalized_existing


def reconcile(
    client: GraphQLClient,
    manifest: dict[str, Any],
    *,
    apply: bool,
    emit: Callable[[str], None] = print,
) -> list[str]:
    mode = "apply" if apply else "dry-run"
    actions: list[str] = []
    owner_id, projects = get_owner_and_projects(client, manifest["owner"])
    matches = [project for project in projects if project.get("title") == manifest["title"]]
    if len(matches) > 1:
        raise BootstrapError(
            f"multiple projects named {manifest['title']!r}; refusing ambiguous update"
        )

    if matches:
        project = matches[0]
    else:
        action = f"create project {manifest['owner']}/{manifest['title']}"
        actions.append(action)
        emit(f"[{mode}] {action}")
        if not apply:
            for field in manifest["fields"]:
                field_action = f"create field {field['name']} ({field['type']})"
                actions.append(field_action)
                emit(f"[{mode}] {field_action}")
            _emit_view_note(manifest, mode, emit)
            return actions
        project = _create_project(client, owner_id, manifest["title"])

    if project.get("number") is not None:
        emit(f"[{mode}] project number: {project['number']}")
    project_id = project["id"]
    fields = get_project_fields(client, project_id)
    by_name: dict[str, dict[str, Any]] = {}
    for field in fields:
        name = field.get("name")
        if name in by_name:
            raise BootstrapError(f"duplicate existing field name: {name}")
        if isinstance(name, str):
            by_name[name] = field

    for desired in manifest["fields"]:
        existing = by_name.get(desired["name"])
        if existing is None:
            action = f"create field {desired['name']} ({desired['type']})"
            actions.append(action)
            emit(f"[{mode}] {action}")
            if apply:
                _create_field(client, project_id, desired)
            continue

        actual_type = existing.get("dataType")
        expected_type = FIELD_TYPES[desired["type"]]
        if actual_type != expected_type:
            raise BootstrapError(
                f"incompatible field {desired['name']!r}: "
                f"GitHub has {actual_type}, manifest requires {expected_type}"
            )
        if desired["type"] == "single_select":
            existing_options = existing.get("options", [])
            if _options_changed(desired["options"], existing_options):
                merged = _merged_options(desired["options"], existing_options)
                action = f"update options for field {desired['name']}"
                actions.append(action)
                emit(f"[{mode}] {action}")
                if apply:
                    data = client.execute(
                        UPDATE_SELECT_FIELD_MUTATION,
                        {"fieldId": existing["id"], "options": merged},
                    )
                    payload = data.get("updateProjectV2Field")
                    updated = (
                        payload.get("projectV2Field")
                        if isinstance(payload, dict)
                        else None
                    )
                    if not isinstance(updated, dict) or not updated.get("id"):
                        raise BootstrapError(
                            f"GitHub did not return updated field {desired['name']!r}"
                        )

    _emit_view_note(manifest, mode, emit)
    if not actions:
        emit(f"[{mode}] project already matches manifest")
    return actions


def _emit_view_note(
    manifest: dict[str, Any], mode: str, emit: Callable[[str], None]
) -> None:
    if manifest.get("views"):
        emit(
            f"[{mode}] views are declarative documentation only; "
            "the GitHub GraphQL API does not expose safe Project v2 view creation"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST, help="manifest JSON path"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create/update resources (default is an accurate read-only dry-run)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        token = os.environ.get(TOKEN_ENV, "")
        client = GraphQLClient(token)
        reconcile(client, manifest, apply=args.apply)
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
