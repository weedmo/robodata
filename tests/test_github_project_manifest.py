"""Contract and unit tests for the GitHub Project v2 bootstrap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.github import bootstrap_project as bootstrap


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / ".github" / "project.json"


def test_manifest_has_expected_name_based_queue_schema():
    manifest = bootstrap.load_manifest(MANIFEST_PATH)

    assert manifest["owner"] == "weedmo"
    assert manifest["title"] == "robodata agent queue"
    assert all("id" not in field for field in manifest["fields"])
    assert "token" not in json.dumps(manifest).lower()

    fields = {field["name"]: field for field in manifest["fields"]}
    expected_selects = {
        "Status": [
            "Inbox",
            "Needs spec",
            "Ready",
            "In progress",
            "In review",
            "Recovery",
            "Blocked",
            "Done",
        ],
        "Type": ["Spec", "Requirement", "Bug"],
        "Priority": ["P0", "P1", "P2", "P3"],
        "Area": [
            "backend",
            "frontend",
            "converter",
            "data",
            "infra",
            "docs",
            "automation",
        ],
        "Risk": ["low", "medium", "high"],
        "Size": ["S", "M", "L"],
        "Validation Profile": [
            "backend",
            "frontend",
            "fullstack",
            "db",
            "docker",
            "docs",
            "submodule",
        ],
    }
    for name, options in expected_selects.items():
        assert fields[name]["type"] == "single_select"
        assert [option["name"] for option in fields[name]["options"]] == options
    assert {name: fields[name]["type"] for name in ("Claim ID", "Agent Run", "Lease Until")} == {
        "Claim ID": "text",
        "Agent Run": "text",
        "Lease Until": "text",
    }
    assert fields["Attempt"]["type"] == "number"
    assert manifest["views"][0]["note"]


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, query, variables):
        self.calls.append((query, variables))
        assert self.responses, "unexpected GraphQL call"
        return self.responses.pop(0)


def _manifest(*fields):
    return {
        "version": 1,
        "owner": "weedmo",
        "title": "robodata agent queue",
        "fields": list(fields),
        "views": [],
    }


def _project_page(nodes, *, has_next=False, cursor=None):
    return {
        "user": {
            "id": "OWNER",
            "projectsV2": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
        }
    }


def _field_page(nodes):
    return {
        "node": {
            "fields": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }


def test_dry_run_plans_missing_project_without_mutations():
    manifest = _manifest(
        {"name": "Claim ID", "type": "text"},
        {
            "name": "Status",
            "type": "single_select",
            "options": [{"name": "Ready", "color": "BLUE", "description": ""}],
        },
    )
    client = FakeClient([_project_page([])])
    output = []

    actions = bootstrap.reconcile(client, manifest, apply=False, emit=output.append)

    assert actions == [
        "create project weedmo/robodata agent queue",
        "create field Claim ID (text)",
        "create field Status (single_select)",
    ]
    assert len(client.calls) == 1
    assert client.calls[0][1] == {"login": "weedmo", "after": None}
    assert all("mutation " not in query for query, _ in client.calls)


def test_apply_creates_project_then_missing_fields_with_variables():
    manifest = _manifest({"name": "Attempt", "type": "number"})
    client = FakeClient(
        [
            _project_page([]),
            {
                "createProjectV2": {
                    "projectV2": {"id": "PROJECT", "title": manifest["title"], "number": 1}
                }
            },
            _field_page([]),
            {"createProjectV2Field": {"projectV2Field": {"id": "FIELD"}}},
        ]
    )

    output = []
    bootstrap.reconcile(client, manifest, apply=True, emit=output.append)

    assert len(client.calls) == 4
    assert "[apply] project number: 1" in output
    assert client.calls[1][1] == {
        "ownerId": "OWNER",
        "title": "robodata agent queue",
    }
    assert client.calls[3][1] == {
        "projectId": "PROJECT",
        "name": "Attempt",
        "dataType": "NUMBER",
        "options": None,
    }
    assert "robodata agent queue" not in client.calls[1][0]
    assert "Attempt" not in client.calls[3][0]


def test_select_update_preserves_extra_existing_options():
    manifest = _manifest(
        {
            "name": "Status",
            "type": "single_select",
            "options": [
                {"name": "Ready", "color": "BLUE", "description": ""},
                {"name": "Done", "color": "GREEN", "description": ""},
            ],
        }
    )
    existing = {
        "id": "STATUS",
        "name": "Status",
        "dataType": "SINGLE_SELECT",
        "options": [
            {"id": "READY", "name": "Ready", "color": "GRAY", "description": None},
            {
                "id": "CUSTOM",
                "name": "Custom",
                "color": "PINK",
                "description": "keep me",
            },
        ],
    }
    client = FakeClient(
        [
            _project_page([{"id": "PROJECT", "title": manifest["title"], "number": 2}]),
            _field_page([existing]),
            {"updateProjectV2Field": {"projectV2Field": {"id": "STATUS"}}},
        ]
    )

    actions = bootstrap.reconcile(client, manifest, apply=True, emit=lambda _: None)

    assert actions == ["update options for field Status"]
    assert client.calls[-1][1]["options"] == [
        {"name": "Ready", "color": "BLUE", "description": "", "id": "READY"},
        {"name": "Done", "color": "GREEN", "description": ""},
        {
            "id": "CUSTOM",
            "name": "Custom",
            "color": "PINK",
            "description": "keep me",
        },
    ]


def test_matching_select_options_are_idempotent_despite_runtime_ids():
    desired = [
        {"name": "Ready", "color": "BLUE", "description": ""},
        {"name": "Done", "color": "GREEN", "description": ""},
    ]
    existing = [
        {"id": "READY", **desired[0]},
        {"id": "DONE", **desired[1]},
    ]

    assert not bootstrap._options_changed(desired, existing)


def test_incompatible_existing_field_fails_without_mutation():
    manifest = _manifest({"name": "Attempt", "type": "number"})
    client = FakeClient(
        [
            _project_page([{"id": "PROJECT", "title": manifest["title"], "number": 2}]),
            _field_page(
                [{"id": "FIELD", "name": "Attempt", "dataType": "TEXT"}]
            ),
        ]
    )

    with pytest.raises(bootstrap.BootstrapError, match="incompatible field"):
        bootstrap.reconcile(client, manifest, apply=True, emit=lambda _: None)
    assert all("mutation " not in query for query, _ in client.calls)


def test_project_lookup_paginates_with_graphql_variables():
    client = FakeClient(
        [
            _project_page([], has_next=True, cursor="CURSOR"),
            _project_page([{"id": "P", "title": "queue", "number": 3}]),
        ]
    )

    owner_id, projects = bootstrap.get_owner_and_projects(client, "weedmo")

    assert owner_id == "OWNER"
    assert projects == [{"id": "P", "title": "queue", "number": 3}]
    assert [variables["after"] for _, variables in client.calls] == [None, "CURSOR"]
    assert all("$login" in query for query, _ in client.calls)


def test_graphql_errors_fail_without_exposing_token():
    token = "super-secret-token"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"errors": [{"message": "permission denied"}]}).encode()

    client = bootstrap.GraphQLClient(token, opener=lambda *_args, **_kwargs: Response())
    with pytest.raises(bootstrap.BootstrapError) as error:
        client.execute("query Test($value: String!) { viewer { login } }", {"value": "x"})

    assert "permission denied" in str(error.value)
    assert token not in str(error.value)


def test_graphql_error_redacts_token_even_if_remote_echoes_it():
    token = "super-secret-token"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {"errors": [{"message": f"rejected credential {token}"}]}
            ).encode()

    client = bootstrap.GraphQLClient(token, opener=lambda *_args, **_kwargs: Response())
    with pytest.raises(bootstrap.BootstrapError) as error:
        client.execute("query Test { viewer { login } }", {})

    assert "[REDACTED]" in str(error.value)
    assert token not in str(error.value)


def test_main_requires_only_named_token_environment(monkeypatch, capsys):
    monkeypatch.delenv(bootstrap.TOKEN_ENV, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-be-used")

    assert bootstrap.main(["--manifest", str(MANIFEST_PATH)]) == 1

    captured = capsys.readouterr()
    assert bootstrap.TOKEN_ENV in captured.err
    assert "must-not-be-used" not in captured.err


@pytest.mark.parametrize(
    "field, message",
    [
        ({"name": "Attempt", "type": "integer"}, "unsupported field type"),
        (
            {
                "name": "Status",
                "type": "single_select",
                "options": [
                    {"name": "Ready", "color": "BLUE", "description": ""},
                    {"name": "Ready", "color": "GREEN", "description": ""},
                ],
            },
            "duplicate option",
        ),
    ],
)
def test_manifest_validation_rejects_invalid_contract(field, message):
    with pytest.raises(bootstrap.BootstrapError, match=message):
        bootstrap.validate_manifest(_manifest(field))
