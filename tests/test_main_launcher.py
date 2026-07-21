"""Static checks for the Docker launcher defaults."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_SH = REPO_ROOT / "main.sh"


def test_default_up_starts_converter_profile():
    script = MAIN_SH.read_text(encoding="utf-8")

    assert "readonly DEFAULT_SERVICES=(app nginx db converter)" in script
    assert "readonly ALL_SERVICES=(app nginx db converter curation-worker)" in script
    assert "default) TARGET_PROFILE_ARGS=(--profile convert) ;;" in script
    assert 'CURATION_DOCKER_PROJECT_NAME="$PROJECT_NAME" docker compose' in script
    assert "app + nginx + db + converter" in script
    assert "printf '\\033[32mup\\033[0m'" in script
    assert "printf '\\033[31mdown\\033[0m'" in script
    assert "Curation Tools Docker Manager" in script
    assert "print_service_row 'converter' 'converter'" in script
    assert "print_service_row 'curation' 'curation-worker'" in script
    assert "print_service_row 'rerun' 'rerun'" not in script
    assert "postgresql://%s:%s@localhost:%s/%s" in script
