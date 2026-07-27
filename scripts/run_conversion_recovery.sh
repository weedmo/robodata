#!/usr/bin/env bash
set -euo pipefail

if (( $# < 2 )); then
  echo "usage: $0 <inspect|rollback|adopt-finalization|quarantine-restart|commit-verified> <cell/task> [options]" >&2
  exit 2
fi

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

"${recovery_compose[@]}" \
  --profile recovery \
  run --rm --no-deps conversion-recovery "$@"
