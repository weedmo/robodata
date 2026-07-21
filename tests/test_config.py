from backend.core.config import Settings


def test_defaults_separate_curation_from_allowed_roots():
    settings = Settings()

    assert settings.dataset_root_base == "/mnt/synology/data/data_div/2026_1"
    assert settings.dataset_sources == ["lerobot", "lerobot_test", "raw"]
    # Path-validation scope is the whole base — any sibling of lerobot is a valid
    # destination for split/sync/merge.
    assert settings.allowed_dataset_roots == ["/mnt/synology/data/data_div/2026_1"]
    # Curation scope (what the UI scans) stays narrow.
    assert settings.configured_dataset_roots() == [
        "/mnt/synology/data/data_div/2026_1/lerobot",
        "/mnt/synology/data/data_div/2026_1/lerobot_test",
        "/mnt/synology/data/data_div/2026_1/raw",
    ]


def test_settings_db_url_defaults_to_local_compose():
    settings = Settings()
    assert (
        settings.db_url
        == "postgresql://curation:dev-only-change-me@127.0.0.1:5433/curation"
    )


def test_settings_db_url_overrides_from_env(monkeypatch):
    monkeypatch.setenv("CURATION_DB_URL", "postgresql://u:p@h:5432/d")

    settings = Settings()
    assert settings.db_url == "postgresql://u:p@h:5432/d"


def test_settings_do_not_expose_rerun_configuration():
    settings = Settings()

    assert not hasattr(settings, "rerun_grpc_port")
    assert not hasattr(settings, "rerun_web_port")
    assert not hasattr(settings, "rerun_grpc_url")
    assert not hasattr(settings, "enable_rerun")
