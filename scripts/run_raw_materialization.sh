#!/usr/bin/env bash
set -euo pipefail

if (( $# != 4 )); then
  echo "usage: $0 <source> <backing> <destination> <manifest>" >&2
  exit 2
fi

source_task="$1"
backing_source="$2"
destination_task="$3"
manifest_path="$4"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
base_compose=(
  docker compose
  -f "${repo_root}/docker/compose.yml"
)
recovery_compose=(
  docker compose
  -f "${repo_root}/docker/compose.yml"
  -f "${repo_root}/docker/compose.conversion-recovery.yml"
)

"${base_compose[@]}" --profile "*" stop app curation-worker converter

running_services="$(
  "${base_compose[@]}" --profile "*" ps --status running --services
)"
for mutation_service in app curation-worker converter; do
  if grep -Fxq "${mutation_service}" <<<"${running_services}"; then
    echo "recovery isolation failed: ${mutation_service} is still running" >&2
    exit 1
  fi
done

active_convert_jobs="$(
  "${base_compose[@]}" exec -T db sh -eu -c \
    'PGOPTIONS="-c default_transaction_read_only=on" psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --no-psqlrc --tuples-only --no-align --set ON_ERROR_STOP=1 --command "$1"' \
    sh \
    "SELECT COUNT(*) FROM jobs WHERE type = 'convert' AND status IN ('queued', 'running', 'cancel_requested');"
)"
if [[ ! "${active_convert_jobs}" =~ ^[[:space:]]*0[[:space:]]*$ ]]; then
  echo "materialization blocked: active convert jobs exist or DB result is invalid: ${active_convert_jobs}" >&2
  exit 1
fi

"${recovery_compose[@]}" \
  --profile recovery \
  run --rm --no-deps --entrypoint /entrypoint.sh conversion-recovery \
  python3 \
  -m scripts.split_raw_task_by_metadata \
  "${source_task}" \
  --materialize-link-view \
  --backing-source "${backing_source}" \
  --detached-destination "${destination_task}" \
  --manifest "${manifest_path}"
