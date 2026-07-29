#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 apply|rollback <raw-root> <lerobot-root> <journal> <source-cell/task> [digest=destination-cell/task ...]" >&2
  exit 2
}

if (( $# < 5 )); then
  usage
fi

operation="$1"
raw_root="$2"
lerobot_root="$3"
journal_path="$4"
source_task="$5"
shift 5

case "${operation}" in
  apply|rollback) ;;
  *) usage ;;
esac

if [[ "${raw_root}" != /* || "${lerobot_root}" != /* || "${journal_path}" != /* ]]; then
  echo "partition state reconciliation paths must be absolute" >&2
  exit 1
fi
if [[ "${source_task}" == /* || "${source_task}" == *".."* || "${source_task}" != */* ]]; then
  echo "partition state source task must be a canonical cell/task path" >&2
  exit 1
fi
for destination in "$@"; do
  if [[ ! "${destination}" =~ ^[0-9a-f]{64}=[^/]+/.+$ ]]; then
    echo "partition state destination must use DIGEST=cell/task" >&2
    exit 1
  fi
  destination_task="${destination#*=}"
  if [[ "${destination_task}" == /* || "${destination_task}" == *".."* || "${destination_task}" == *"//"* || "${destination_task}" == *"/./"* ]]; then
    echo "partition state destination task must be canonical" >&2
    exit 1
  fi
done

canonical_raw="$(realpath -m -- "${raw_root}")"
canonical_lerobot="$(realpath -m -- "${lerobot_root}")"
canonical_journal="$(realpath -m -- "${journal_path}")"
if [[ "${canonical_raw}" != "${raw_root}" || "${canonical_lerobot}" != "${lerobot_root}" || "${canonical_journal}" != "${journal_path}" ]]; then
  echo "partition state reconciliation paths must be canonical" >&2
  exit 1
fi

data_root="${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}"
canonical_data_root="$(realpath -m -- "${data_root}")"
for protected_path in "${raw_root}" "${lerobot_root}" "${journal_path}"; do
  if [[ "${protected_path}" != "${canonical_data_root}"/* ]]; then
    echo "partition state path is outside CURATION_DATA_ROOT: ${protected_path}" >&2
    exit 1
  fi
done

source_path="${raw_root}/${source_task}"
state_path="${lerobot_root}/convert_state.json"
journal_dir="$(dirname -- "${journal_path}")"
journal_name="$(basename -- "${journal_path}")"
state_log="${journal_dir}/.${journal_name}.state-log"

if [[ ! -d "${raw_root}" || -L "${raw_root}" || ! -d "${lerobot_root}" || -L "${lerobot_root}" ]]; then
  echo "raw and lerobot roots must be plain directories" >&2
  exit 1
fi
if [[ ! -d "${source_path}" || -L "${source_path}" ]]; then
  echo "partition state source must be a plain directory: ${source_path}" >&2
  exit 1
fi
for protected_file in "${state_path}" "${journal_path}" "${state_log}"; do
  if [[ ! -f "${protected_file}" || -L "${protected_file}" ]]; then
    echo "partition state authority must be a plain file: ${protected_file}" >&2
    exit 1
  fi
done

state_uid="$(stat -c '%u' -- "${state_path}")"
state_gid="$(stat -c '%g' -- "${state_path}")"
source_uid="$(stat -c '%u' -- "${source_path}")"
source_gid="$(stat -c '%g' -- "${source_path}")"
if [[ ! "${source_uid}" =~ ^[0-9]+$ || ! "${source_gid}" =~ ^[0-9]+$ ]]; then
  echo "partition state source owner must resolve to numeric UID:GID" >&2
  exit 1
fi
for authority_path in "${journal_path}" "${state_log}"; do
  authority_uid="$(stat -c '%u' -- "${authority_path}")"
  authority_gid="$(stat -c '%g' -- "${authority_path}")"
  if [[ "${authority_uid}:${authority_gid}" != "${state_uid}:${state_gid}" ]]; then
    echo "partition state artifact owners are incompatible; journal, state-log, and canonical state must share UID:GID" >&2
    exit 1
  fi
done
if [[ "$(stat -c '%a' -- "${journal_path}")" != "600" || "$(stat -c '%a' -- "${state_log}")" != "600" ]]; then
  echo "partition journal and state-log must be mode 0600" >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ ! -d "${runtime_dir}" || -L "${runtime_dir}" ]]; then
  echo "partition state runtime lock directory is unavailable: ${runtime_dir}" >&2
  exit 1
fi
if [[ "$(stat -c '%u' -- "${runtime_dir}")" != "$(id -u)" || "$(stat -c '%a' -- "${runtime_dir}")" != "700" ]]; then
  echo "partition state runtime lock directory must be owned by the caller and mode 0700" >&2
  exit 1
fi
umask 077
exec 9<"${runtime_dir}"
if ! flock -n 9; then
  echo "another offline conversion mutation wrapper is already running" >&2
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
    echo "partition state isolation failed: ${mutation_service} is still running" >&2
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
  echo "partition state reconciliation blocked: active convert jobs exist or DB result is invalid: ${active_convert_jobs}" >&2
  exit 1
fi

module_args=(
  python3
  -m
  scripts.reconcile_partition_convert_state
  "${operation}"
  "${raw_root}"
  "${lerobot_root}"
  "${journal_path}"
  "${source_task}"
)
for destination in "$@"; do
  module_args+=(--destination "${destination}")
done

"${recovery_compose[@]}" \
  --profile recovery \
  run \
  --rm \
  --no-deps \
  --build \
  --user "${state_uid}:${state_gid}" \
  --entrypoint /entrypoint.sh \
  --volume "${raw_root}:${raw_root}:ro" \
  --volume "${lerobot_root}:${lerobot_root}:rw" \
  --volume "${journal_path}:${journal_path}:ro" \
  --volume "${state_log}:${state_log}:ro" \
  conversion-recovery \
  "${module_args[@]}"
