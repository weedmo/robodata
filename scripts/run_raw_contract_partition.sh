#!/usr/bin/env bash
set -euo pipefail

if (( $# < 1 )); then
  echo "usage: $0 apply <source> <contract> <journal> <keep-digest> [digest=destination ...] | rollback <source> <journal>" >&2
  exit 2
fi

operation="$1"
shift
case "${operation}" in
  apply)
    if (( $# < 4 )); then
      echo "usage: $0 apply <source> <contract> <journal> <keep-digest> [digest=destination ...]" >&2
      exit 2
    fi
    source_task="$1"
    contract_manifest="$2"
    journal_path="$3"
    keep_digest="$4"
    shift 4
    ;;
  rollback)
    if (( $# != 2 )); then
      echo "usage: $0 rollback <source> <journal>" >&2
      exit 2
    fi
    source_task="$1"
    journal_path="$2"
    ;;
  *)
    echo "unsupported operation: ${operation}" >&2
    exit 2
    ;;
esac

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ ! -d "${runtime_dir}" || -L "${runtime_dir}" ]]; then
  echo "contract partition runtime lock directory is unavailable: ${runtime_dir}" >&2
  exit 1
fi
if [[ "$(stat -c '%u' -- "${runtime_dir}")" != "$(id -u)" ]]; then
  echo "contract partition runtime lock directory has a foreign owner" >&2
  exit 1
fi
if [[ "$(stat -c '%a' -- "${runtime_dir}")" != "700" ]]; then
  echo "contract partition runtime lock directory must be mode 0700" >&2
  exit 1
fi
umask 077
exec 9<"${runtime_dir}"
if ! flock -n 9; then
  echo "another raw contract partition wrapper is already running" >&2
  exit 1
fi

if [[ ! -d "${source_task}" || -L "${source_task}" ]]; then
  echo "contract partition source must be a plain directory: ${source_task}" >&2
  exit 1
fi
source_uid="$(stat -c '%u' -- "${source_task}")"
source_gid="$(stat -c '%g' -- "${source_task}")"
if [[ ! "${source_uid}" =~ ^[0-9]+$ || ! "${source_gid}" =~ ^[0-9]+$ ]]; then
  echo "contract partition source owner must resolve to numeric UID:GID" >&2
  exit 1
fi
journal_parent="$(dirname -- "${journal_path}")"
if [[ ! -d "${journal_parent}" || -L "${journal_parent}" ]]; then
  echo "contract partition journal parent must be a plain directory" >&2
  exit 1
fi
artifact_uid="$(stat -c '%u' -- "${journal_parent}")"
artifact_gid="$(stat -c '%g' -- "${journal_parent}")"
if [[ "$(stat -c '%a' -- "${journal_parent}")" != "700" ]]; then
  echo "contract partition journal parent must be mode 0700" >&2
  exit 1
fi

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
    echo "contract partition isolation failed: ${mutation_service} is still running" >&2
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
  echo "contract partition blocked: active convert jobs exist or DB result is invalid: ${active_convert_jobs}" >&2
  exit 1
fi

run_args=(
  run
  --rm
  --no-deps
  --build
  --user "${artifact_uid}:${artifact_gid}"
  --entrypoint /entrypoint.sh
)
module_args=(
  python3
  -m scripts.partition_raw_by_contract
)

if [[ "${operation}" == "apply" ]]; then
  if [[ ! -f "${contract_manifest}" || -L "${contract_manifest}" ]]; then
    echo "contract partition manifest must be a plain file: ${contract_manifest}" >&2
    exit 1
  fi
  run_args+=(
    --volume "${contract_manifest}:/contract-manifest.json:ro"
  )
  module_args+=(
    apply
    "${source_task}"
    /contract-manifest.json
    "${journal_path}"
    "${keep_digest}"
    --artifact-uid "${artifact_uid}"
    --artifact-gid "${artifact_gid}"
  )
  for destination in "$@"; do
    module_args+=(--destination "${destination}")
  done
else
  module_args+=(
    rollback
    "${source_task}"
    "${journal_path}"
    --artifact-uid "${artifact_uid}"
    --artifact-gid "${artifact_gid}"
  )
fi

"${recovery_compose[@]}" \
  --profile recovery \
  "${run_args[@]}" \
  conversion-recovery \
  "${module_args[@]}"
