#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TIMESTAMP="$(date +%Y%m%dT%H%M%S)"
DEST_DIR="$REPO_ROOT/docs/db-backup/$TIMESTAMP"
MANIFEST_PATH="$DEST_DIR/MANIFEST.txt"
HOST_DB_PATH="$HOME/.local/share/curation-tools/metadata.db"
ARTIFACTS_FOUND=0

mkdir -p "$DEST_DIR"
: > "$MANIFEST_PATH"

record_artifact() {
  local artifact_path="$1"
  local size
  local mtime
  local sha256

  size="$(stat -c %s "$artifact_path")"
  mtime="$(stat -c %y "$artifact_path")"
  sha256="$(sha256sum "$artifact_path" | awk '{print $1}')"

  printf "%s\nsize=%s\nmtime=%s\nsha256=%s\n\n" \
    "$(basename "$artifact_path")" "$size" "$mtime" "$sha256" >> "$MANIFEST_PATH"
  ARTIFACTS_FOUND=1
}

copy_if_present() {
  local source_path="$1"
  local dest_name="$2"

  if [[ -f "$source_path" ]]; then
    cp -a "$source_path" "$DEST_DIR/$dest_name"
    record_artifact "$DEST_DIR/$dest_name"
  fi
}

backup_volume_if_present() {
  if ! command -v docker >/dev/null 2>&1; then
    return
  fi

  if docker volume inspect ui_service_db >/dev/null 2>&1; then
    docker run --rm \
      -v ui_service_db:/src:ro \
      -v "$DEST_DIR":/out \
      alpine \
      tar czf /out/ui_service_db.tar.gz -C /src .
    record_artifact "$DEST_DIR/ui_service_db.tar.gz"
  fi
}

copy_if_present "$HOST_DB_PATH" "host-metadata.db"

if [[ -n "${CURATION_DB_PATH:-}" && -f "$CURATION_DB_PATH" ]]; then
  if [[ ! -f "$HOST_DB_PATH" || ! "$CURATION_DB_PATH" -ef "$HOST_DB_PATH" ]]; then
    copy_if_present "$CURATION_DB_PATH" "env-metadata.db"
  fi
fi

backup_volume_if_present

if [[ "$ARTIFACTS_FOUND" -eq 0 ]]; then
  rm -f "$MANIFEST_PATH"
  rmdir "$DEST_DIR"
  echo "No SQLite artifacts found; nothing to back up."
  exit 0
fi

echo "Backup complete: $DEST_DIR"
echo "Manifest:"
cat "$MANIFEST_PATH"
