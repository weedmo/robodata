#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}"
RAW_ROOT_EXPLICIT=0
if [[ -n "${RAW_ROOT:-}" ]]; then
  RAW_ROOT_EXPLICIT=1
fi
RAW_ROOT="${RAW_ROOT:-${DATA_ROOT%/}/raw}"
DRY_RUN=0
USE_SUDO=auto
CELL_TIMEOUT="${CELL_TIMEOUT:-120}"

usage() {
  cat <<'USAGE'
Fix raw-data permissions so curation containers can scan every raw/cell* tree.

Usage:
  scripts/fix_raw_permissions.sh [--raw-root PATH] [--data-root PATH] [--dry-run] [--sudo|--no-sudo]

Defaults:
  CURATION_DATA_ROOT=/mnt/synology/data/data_div/2026_1
  RAW_ROOT=$CURATION_DATA_ROOT/raw

What it changes:
  - directories: u+rwX,g+rwX,o+rX
  - files:       u+rw,g+rw,o+r
  - skips hidden control folders such as .omx

Examples:
  scripts/fix_raw_permissions.sh --dry-run
  scripts/fix_raw_permissions.sh
  CURATION_DATA_ROOT=/mnt/synology/data/data_div/2026_1 scripts/fix_raw_permissions.sh
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-root)
      DATA_ROOT="${2:?missing value for --data-root}"
      if [[ $RAW_ROOT_EXPLICIT -eq 0 ]]; then
        RAW_ROOT="${DATA_ROOT%/}/raw"
      fi
      shift 2
      ;;
    --raw-root)
      RAW_ROOT="${2:?missing value for --raw-root}"
      RAW_ROOT_EXPLICIT=1
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --sudo)
      USE_SUDO=yes
      shift
      ;;
    --no-sudo)
      USE_SUDO=no
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

RAW_ROOT="${RAW_ROOT%/}"

if [[ ! -d "$RAW_ROOT" ]]; then
  echo "Raw root does not exist or is not a directory: $RAW_ROOT" >&2
  exit 1
fi

if ! find "$RAW_ROOT" -maxdepth 0 -type d >/dev/null 2>&1; then
  echo "Cannot access raw root: $RAW_ROOT" >&2
  exit 1
fi

SUDO=()
if [[ "$USE_SUDO" == "yes" ]]; then
  SUDO=(sudo)
elif [[ "$USE_SUDO" == "auto" && $EUID -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO=(sudo)
  fi
fi

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

echo "Raw root: $RAW_ROOT"
if [[ ${#SUDO[@]} -gt 0 ]]; then
  echo "Using sudo for permission changes. Enter your sudo password if prompted."
fi

echo "Scanning cell directories..."
mapfile -d '' CELL_DIRS < <(
  find "$RAW_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'cell*' -print0 | sort -z
)

if [[ ${#CELL_DIRS[@]} -eq 0 ]]; then
  echo "No cell directories found under $RAW_ROOT"
  exit 0
fi

echo "Found ${#CELL_DIRS[@]} cell directories."

FAILED_CELLS=()

for cell_dir in "${CELL_DIRS[@]}"; do
  echo "Fixing: $cell_dir"

  if [[ $DRY_RUN -eq 1 ]]; then
    run "${SUDO[@]}" find "$cell_dir" \
      -path '*/.omx' -prune -o \
      -type d -exec chmod u+rwX,g+rwX,o+rX {} +
    run "${SUDO[@]}" find "$cell_dir" \
      -path '*/.omx' -prune -o \
      -type f -exec chmod u+rw,g+rw,o+r {} +
    continue
  fi

  cell_errors="$(mktemp)"
  cell_failed=0

  # Directory traversal requires execute permission on every directory.
  if ! timeout "$CELL_TIMEOUT" "${SUDO[@]}" find "$cell_dir" \
    -path '*/.omx' -prune -o \
    -type d -exec chmod u+rwX,g+rwX,o+rX {} + 2>"$cell_errors"; then
    cell_failed=1
  fi

  # Raw metadata and mcap files only need read access for scanning and viewing.
  if ! timeout "$CELL_TIMEOUT" "${SUDO[@]}" find "$cell_dir" \
    -path '*/.omx' -prune -o \
    -type f -exec chmod u+rw,g+rw,o+r {} + 2>>"$cell_errors"; then
    cell_failed=1
  fi

  if [[ $cell_failed -ne 0 ]]; then
    error_count="$(wc -l <"$cell_errors")"
    echo "Failed: $cell_dir ($error_count errors; showing first 5)" >&2
    sed -n '1,5p' "$cell_errors" >&2
    FAILED_CELLS+=("$cell_dir")
  fi
  rm -f "$cell_errors"
done

echo "Verifying unreadable paths..."
if [[ $DRY_RUN -eq 1 ]]; then
  echo "Dry run complete; no permissions changed, so verification was skipped."
  exit 0
fi

VERIFY_FAILURES=()
for cell_dir in "${CELL_DIRS[@]}"; do
  verify_output="$(mktemp)"
  if ! timeout "$CELL_TIMEOUT" find "$cell_dir" \
    -path '*/.omx' -prune -o \
    \( -type d ! -readable -o -type d ! -executable -o -type f ! -readable \) \
    -print >"$verify_output" 2>&1; then
    VERIFY_FAILURES+=("$cell_dir")
  elif [[ -s "$verify_output" ]]; then
    VERIFY_FAILURES+=("$cell_dir")
  fi
  rm -f "$verify_output"
done

if [[ ${#FAILED_CELLS[@]} -gt 0 || ${#VERIFY_FAILURES[@]} -gt 0 ]]; then
  echo "Permission fix incomplete." >&2
  if [[ ${#FAILED_CELLS[@]} -gt 0 ]]; then
    printf '  chmod failed: %s\n' "${FAILED_CELLS[@]}" >&2
  fi
  if [[ ${#VERIFY_FAILURES[@]} -gt 0 ]]; then
    printf '  verification failed: %s\n' "${VERIFY_FAILURES[@]}" >&2
  fi
  echo "The NFS server owner/ACL must grant group read and directory execute permissions." >&2
  exit 1
fi

echo "Raw permission fix complete."
