from backend.core.config import Settings


def test_defaults_separate_curation_from_allowed_roots():
    settings = Settings()

    assert settings.dataset_root_base == "/mnt/synology/data/data_div/2026_1"
    assert settings.dataset_sources == ["lerobot", "lerobot_test"]
    # Path-validation scope is the whole base — any sibling of lerobot is a valid
    # destination for split/sync/merge.
    assert settings.allowed_dataset_roots == ["/mnt/synology/data/data_div/2026_1"]
    # Curation scope (what the UI scans) stays narrow.
    assert settings.configured_dataset_roots() == [
        "/mnt/synology/data/data_div/2026_1/lerobot",
        "/mnt/synology/data/data_div/2026_1/lerobot_test",
    ]
