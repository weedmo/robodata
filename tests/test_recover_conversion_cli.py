"""Focused contract tests for the offline conversion recovery CLI."""

from __future__ import annotations

import json
from pathlib import Path

import scripts.recover_conversion as cli


class _FakeRecoveryService:
    instances: list["_FakeRecoveryService"] = []

    def __init__(
        self,
        *,
        raw_root: Path,
        lerobot_root: Path,
        state_file: Path | None,
        authorized_legacy_marker_sha256s: set[str],
        contract_manifest_path: Path | None,
        authorized_contract_manifest_sha256: str | None,
    ) -> None:
        self.raw_root = raw_root
        self.lerobot_root = lerobot_root
        self.state_file = state_file
        self.authorized_legacy_marker_sha256s = (
            authorized_legacy_marker_sha256s
        )
        self.contract_manifest_path = contract_manifest_path
        self.authorized_contract_manifest_sha256 = (
            authorized_contract_manifest_sha256
        )
        self.calls: list[tuple[str, ...]] = []
        self.instances.append(self)

    def inspect(self, cell_task: str) -> dict[str, object]:
        self.calls.append(("inspect", cell_task))
        return {"cell_task": cell_task, "phase": "pending"}

    def recover(self, cell_task: str, mode: str) -> dict[str, object]:
        self.calls.append(("recover", cell_task, mode))
        return {"cell_task": cell_task, "mode": mode, "phase": "complete"}


def test_inspect_emits_one_json_document_and_passes_explicit_roots(
    tmp_path, monkeypatch, capsys
):
    _FakeRecoveryService.instances.clear()
    monkeypatch.setattr(
        cli, "_recovery_service_class", lambda: _FakeRecoveryService
    )
    state_file = tmp_path / "state.json"

    result = cli.main(
        [
            "inspect",
            "cell007/example_task",
            "--raw-root",
            str(tmp_path / "raw"),
            "--lerobot-root",
            str(tmp_path / "lerobot"),
            "--state-file",
            str(state_file),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "cell_task": "cell007/example_task",
        "phase": "pending",
    }
    assert captured.out.count("\n") == 1
    service = _FakeRecoveryService.instances[-1]
    assert service.raw_root == tmp_path / "raw"
    assert service.lerobot_root == tmp_path / "lerobot"
    assert service.state_file == state_file
    assert service.authorized_legacy_marker_sha256s == set()
    assert service.contract_manifest_path is None
    assert service.authorized_contract_manifest_sha256 is None
    assert service.calls == [("inspect", "cell007/example_task")]


def test_mutating_mode_uses_recover(monkeypatch, capsys):
    _FakeRecoveryService.instances.clear()
    monkeypatch.setattr(
        cli, "_recovery_service_class", lambda: _FakeRecoveryService
    )

    result = cli.main(["quarantine-restart", "cell004/example_task"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert json.loads(captured.out)["phase"] == "complete"
    assert _FakeRecoveryService.instances[-1].calls == [
        ("recover", "cell004/example_task", "quarantine-restart")
    ]


def test_repeated_legacy_marker_authorizations_are_passed_to_service(
    monkeypatch,
    capsys,
):
    _FakeRecoveryService.instances.clear()
    monkeypatch.setattr(
        cli, "_recovery_service_class", lambda: _FakeRecoveryService
    )
    first = "1" * 64
    second = "2" * 64

    assert (
        cli.main(
            [
                "inspect",
                "cell007/example_task",
                "--authorize-legacy-marker-sha256",
                first,
                "--authorize-legacy-marker-sha256",
                second,
            ]
        )
        == 0
    )

    capsys.readouterr()
    assert _FakeRecoveryService.instances[
        -1
    ].authorized_legacy_marker_sha256s == {first, second}


def test_contract_manifest_authorization_is_passed_as_an_exact_pair(
    tmp_path,
    monkeypatch,
    capsys,
):
    _FakeRecoveryService.instances.clear()
    monkeypatch.setattr(
        cli, "_recovery_service_class", lambda: _FakeRecoveryService
    )
    manifest = tmp_path / "contract-manifest.json"
    digest = "a" * 64

    assert (
        cli.main(
            [
                "inspect",
                "cell007/example_task",
                "--contract-manifest",
                str(manifest),
                "--authorize-contract-manifest-sha256",
                digest,
            ]
        )
        == 0
    )

    capsys.readouterr()
    service = _FakeRecoveryService.instances[-1]
    assert service.contract_manifest_path == manifest
    assert service.authorized_contract_manifest_sha256 == digest


def test_contract_manifest_authorization_flags_must_be_paired(capsys):
    for unpaired_args in (
        ["--contract-manifest", "/secure/contract-manifest.json"],
        ["--authorize-contract-manifest-sha256", "a" * 64],
    ):
        try:
            cli.main(["inspect", "cell007/example_task", *unpaired_args])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("unpaired contract authorization was accepted")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must be provided together" in captured.err


def test_contract_manifest_authorization_requires_lowercase_sha256(capsys):
    for invalid_digest in ("A" * 64, "a" * 63, "g" * 64):
        try:
            cli.main(
                [
                    "inspect",
                    "cell007/example_task",
                    "--contract-manifest",
                    "/secure/contract-manifest.json",
                    "--authorize-contract-manifest-sha256",
                    invalid_digest,
                ]
            )
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("invalid contract authorization was accepted")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "lowercase SHA-256" in captured.err


def test_default_roots_follow_container_environment(tmp_path, monkeypatch, capsys):
    _FakeRecoveryService.instances.clear()
    monkeypatch.setattr(
        cli, "_recovery_service_class", lambda: _FakeRecoveryService
    )
    monkeypatch.setenv("RAW_BASE", str(tmp_path / "mounted-raw"))
    monkeypatch.setenv("LEROBOT_BASE", str(tmp_path / "mounted-lerobot"))

    assert cli.main(["inspect", "cell007/example_task"]) == 0

    capsys.readouterr()
    service = _FakeRecoveryService.instances[-1]
    assert service.raw_root == tmp_path / "mounted-raw"
    assert service.lerobot_root == tmp_path / "mounted-lerobot"
    assert service.state_file is None


def test_service_error_uses_stderr_and_nonzero_exit(monkeypatch, capsys):
    class FailingRecoveryService(_FakeRecoveryService):
        def inspect(self, cell_task: str) -> dict[str, object]:
            raise RuntimeError("ambiguous recovery state")

    monkeypatch.setattr(
        cli, "_recovery_service_class", lambda: FailingRecoveryService
    )

    result = cli.main(["inspect", "cell007/example_task"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "RuntimeError: ambiguous recovery state" in captured.err
