#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

resolve_path() {
  local path="$1"
  if [[ "$path" = /* ]]; then
    printf '%s\n' "$path"
  else
    printf '%s\n' "$SCRIPT_DIR/$path"
  fi
}

PROJECT_NAME="${PROJECT_NAME:-curation-tools}"
COMPOSE_FILE="$(resolve_path "${COMPOSE_FILE:-docker/compose.yml}")"
ENV_FILE="$(resolve_path "${ENV_FILE:-docker/.env}")"
ENV_EXAMPLE_FILE="$(resolve_path "${ENV_EXAMPLE_FILE:-docker/.env.example}")"
BACKUP_SCRIPT="$(resolve_path "${BACKUP_SCRIPT:-scripts/backup_sqlite_metadata.sh}")"

DB_VOLUME_KEY="${DB_VOLUME_KEY:-curation_pg_data}"

readonly DEFAULT_SERVICES=(app nginx db converter)
readonly ALL_SERVICES=(app nginx db converter curation-worker)

export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

log() {
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<EOF
Usage: ./main.sh [command]

Commands:
  --up
  --up-convert
  --up-curator
  --up-all
  --down
  --build
  --build-nocache
  --logs [service]
  --shell <app|converter|curation-worker|db>
  --psql
  --backup-sqlite
  --reset-db [--yes]
EOF
}

ensure_env_file() {
  [[ -f "$ENV_EXAMPLE_FILE" ]] || die "Missing env example: $ENV_EXAMPLE_FILE"
  if [[ -f "$ENV_FILE" ]]; then
    return 0
  fi

  cp "$ENV_EXAMPLE_FILE" "$ENV_FILE"
  cat <<EOF
Created $ENV_FILE from $ENV_EXAMPLE_FILE.
Edit POSTGRES_PASSWORD in $ENV_FILE, then rerun this script.
EOF
  exit 1
}

preflight() {
  command -v docker >/dev/null 2>&1 || die "docker not found"
  docker compose version >/dev/null 2>&1 || die "'docker compose' plugin not available"
  [[ -f "$COMPOSE_FILE" ]] || die "Missing compose file: $COMPOSE_FILE"
  ensure_env_file
}

compose() {
  CURATION_DOCKER_PROJECT_NAME="$PROJECT_NAME" docker compose --env-file "$ENV_FILE" -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
}

read_env_file_value() {
  local key="$1"
  local line raw_key raw_value

  [[ -f "$ENV_FILE" ]] || return 1

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    line="${line#export }"
    [[ "$line" == *=* ]] || continue

    raw_key="${line%%=*}"
    raw_value="${line#*=}"
    raw_key="${raw_key#"${raw_key%%[![:space:]]*}"}"
    raw_key="${raw_key%"${raw_key##*[![:space:]]}"}"

    if [[ "$raw_key" != "$key" ]]; then
      continue
    fi

    raw_value="${raw_value#"${raw_value%%[![:space:]]*}"}"
    raw_value="${raw_value%"${raw_value##*[![:space:]]}"}"
    if [[ "$raw_value" == \"*\" && "$raw_value" == *\" ]]; then
      raw_value="${raw_value:1:${#raw_value}-2}"
    elif [[ "$raw_value" == \'*\' && "$raw_value" == *\' ]]; then
      raw_value="${raw_value:1:${#raw_value}-2}"
    fi

    printf '%s\n' "$raw_value"
    return 0
  done < "$ENV_FILE"

  return 1
}

resolve_setting() {
  local key="$1"
  local default_value="$2"
  local env_value=

  if [[ -v "$key" ]]; then
    printf '%s\n' "${!key}"
    return 0
  fi

  if env_value="$(read_env_file_value "$key")"; then
    printf '%s\n' "$env_value"
    return 0
  fi

  printf '%s\n' "$default_value"
}

set_target_profiles() {
  TARGET_PROFILE_ARGS=()
  case "$1" in
    default) TARGET_PROFILE_ARGS=(--profile convert) ;;
    convert) TARGET_PROFILE_ARGS=(--profile convert) ;;
    curator) TARGET_PROFILE_ARGS=(--profile curator) ;;
    all) TARGET_PROFILE_ARGS=(--profile convert --profile curator) ;;
    *)
      die "Unknown target: $1"
      ;;
  esac
}

set_service_profile() {
  SERVICE_PROFILE_ARGS=()
  case "$1" in
    converter) SERVICE_PROFILE_ARGS=(--profile convert) ;;
    curation-worker) SERVICE_PROFILE_ARGS=(--profile curator) ;;
  esac
}

service_running() {
  local service="$1"
  compose ps --status running --services "$service" 2>/dev/null | grep -Fxq "$service"
}

service_status() {
  local service="$1"
  if service_running "$service"; then
    if [[ -t 1 ]]; then
      printf '\033[32mup\033[0m'
    else
      printf 'up'
    fi
  else
    if [[ -t 1 ]]; then
      printf '\033[31mdown\033[0m'
    else
      printf 'down'
    fi
  fi
}

service_icon() {
  local service="$1"
  if service_running "$service"; then
    printf '🟢'
  else
    printf '🔴'
  fi
}

default_container_name() {
  local service="$1"
  case "$service" in
    converter) printf 'convert-server' ;;
    *) printf '%s-%s-1' "$PROJECT_NAME" "$service" ;;
  esac
}

service_container_name() {
  local service="$1"
  local container_id container_name

  container_id="$(compose ps -q "$service" 2>/dev/null || true)"
  if [[ -n "$container_id" ]]; then
    container_name="$(docker inspect --format '{{.Name}}' "$container_id" 2>/dev/null || true)"
    container_name="${container_name#/}"
    if [[ -n "$container_name" ]]; then
      printf '%s' "$container_name"
      return 0
    fi
  fi

  default_container_name "$service"
}

print_service_row() {
  local label="$1"
  local service="$2"

  printf '  │  %-12s %s  %-35s │\n' "$label" "$(service_icon "$service")" "$(service_container_name "$service")"
}

require_known_service() {
  local service="$1"
  case "$service" in
    app|nginx|db|converter|curation-worker) ;;
    *)
      die "Unknown service: $service"
      ;;
  esac
}

compose_exec() {
  if [[ -t 0 ]]; then
    compose exec "$@"
  else
    compose exec -T "$@"
  fi
}

warn_if_convert_profile_not_isolated() {
  local target="$1"
  if [[ "$PROJECT_NAME" != "curation-tools" ]] && [[ "$target" == "convert" || "$target" == "all" ]]; then
    log "WARN: converter uses fixed container_name 'convert-server'; PROJECT_NAME alone does not isolate that service."
  fi
}

up_target() {
  local target="$1"
  local data_root
  preflight
  set_target_profiles "$target"
  warn_if_convert_profile_not_isolated "$target"

  data_root="$(resolve_setting CURATION_DATA_ROOT /mnt/synology/data/data_div/2026_1)"
  [[ -d "$data_root" ]] || log "WARN: data root '$data_root' does not exist on the host"
  compose "${TARGET_PROFILE_ARGS[@]}" up -d
}

build_all() {
  local nocache="${1:-false}"
  preflight
  set_target_profiles all

  if [[ "$nocache" == "true" ]]; then
    compose "${TARGET_PROFILE_ARGS[@]}" build --no-cache
  else
    compose "${TARGET_PROFILE_ARGS[@]}" build
  fi
}

down_all() {
  preflight
  set_target_profiles all
  compose "${TARGET_PROFILE_ARGS[@]}" down --remove-orphans
}

logs_cmd() {
  local service="${1:-}"
  preflight
  set_target_profiles all

  if [[ -n "$service" ]]; then
    require_known_service "$service"
    compose "${TARGET_PROFILE_ARGS[@]}" logs -f --tail=100 "$service"
  else
    compose "${TARGET_PROFILE_ARGS[@]}" logs -f --tail=100
  fi
}

ensure_db_running() {
  if service_running db; then
    wait_for_service_ready db
    return 0
  fi
  log "Starting db service for project '$PROJECT_NAME'..."
  compose up -d db
  wait_for_service_ready db
}

wait_for_service_ready() {
  local service="$1"
  local timeout_seconds="${2:-60}"
  local elapsed=0
  local container_id=
  local status=

  while (( elapsed < timeout_seconds )); do
    container_id="$(compose ps -q "$service" 2>/dev/null || true)"
    if [[ -n "$container_id" ]]; then
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      case "$status" in
        healthy|running)
          return 0
          ;;
        unhealthy|dead|exited)
          break
          ;;
      esac
    fi

    sleep 1
    elapsed=$((elapsed + 1))
  done

  die "Service '$service' did not become ready"
}

open_shell() {
  local service="$1"
  local shell_cmd='if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi'

  preflight
  require_known_service "$service"

  if service_running "$service"; then
    compose_exec "$service" sh -lc "$shell_cmd"
    return 0
  fi

  if [[ "$service" == "db" ]]; then
    ensure_db_running
    compose_exec db sh -lc "$shell_cmd"
    return 0
  fi

  set_service_profile "$service"
  compose "${SERVICE_PROFILE_ARGS[@]}" run --rm --entrypoint sh "$service" -lc "$shell_cmd"
}

psql_cmd() {
  local postgres_db postgres_user
  preflight
  ensure_db_running
  postgres_db="$(resolve_setting POSTGRES_DB curation)"
  postgres_user="$(resolve_setting POSTGRES_USER curation)"
  compose_exec db sh -lc "PGPASSWORD=\"\${POSTGRES_PASSWORD:-}\" exec psql -h 127.0.0.1 -U \"$postgres_user\" -d \"$postgres_db\""
}

backup_sqlite() {
  preflight
  [[ -f "$BACKUP_SCRIPT" ]] || die "Missing backup script: $BACKUP_SCRIPT"
  "$BACKUP_SCRIPT"
}

project_db_volume_name() {
  printf '%s_%s\n' "$PROJECT_NAME" "$DB_VOLUME_KEY"
}

confirm_reset() {
  local volume_name="$1"
  printf "Reset DB volume '%s' for project '%s'? [y/N] " "$volume_name" "$PROJECT_NAME"
  read -r answer
  [[ "$answer" == "y" || "$answer" == "Y" ]]
}

reset_db() {
  local skip_confirm="${1:-false}"
  local volume_name

  preflight
  volume_name="$(project_db_volume_name)"

  if [[ "$skip_confirm" != "true" ]] && ! confirm_reset "$volume_name"; then
    log "Reset cancelled."
    return 0
  fi

  log "Stopping compose project '$PROJECT_NAME' before DB reset..."
  down_all || true

  if docker volume inspect "$volume_name" >/dev/null 2>&1; then
    log "Removing volume '$volume_name'..."
    docker volume rm "$volume_name" >/dev/null
  else
    log "Volume '$volume_name' does not exist yet; continuing."
  fi

  log "Re-initializing db service..."
  compose up -d db
  wait_for_service_ready db
}

show_menu() {
  local ui_port data_root db_port postgres_db postgres_user postgres_password
  ui_port="$(resolve_setting CURATION_UI_PORT 18080)"
  data_root="$(resolve_setting CURATION_DATA_ROOT /mnt/synology/data/data_div/2026_1)"
  db_port="$(resolve_setting CURATION_PG_HOST_PORT 127.0.0.1:5433)"
  db_port="${db_port##*:}"
  postgres_db="$(resolve_setting POSTGRES_DB curation)"
  postgres_user="$(resolve_setting POSTGRES_USER curation)"
  postgres_password="$(resolve_setting POSTGRES_PASSWORD password)"

  printf '\n'
  printf '╔══════════════════════════════════════════════╗\n'
  printf '║       Curation Tools Docker Manager         ║\n'
  printf '╚══════════════════════════════════════════════╝\n'
  printf '\n'
  printf '  ┌──────────────────────────────────────────────────────┐\n'
  print_service_row 'postgres' 'db'
  print_service_row 'server' 'app'
  print_service_row 'client' 'nginx'
  print_service_row 'converter' 'converter'
  print_service_row 'curation' 'curation-worker'
  printf '  └──────────────────────────────────────────────────────┘\n'
  printf '\n'
  printf '  DB       : postgresql://%s:%s@localhost:%s/%s\n' "$postgres_user" "$postgres_password" "$db_port" "$postgres_db"
  printf '  Web      : http://localhost:%s\n' "$ui_port"
  printf '  API      : http://localhost:%s/api/health\n' "$ui_port"
  printf '  Converter: http://localhost:%s/converter\n' "$ui_port"
  printf '  Data     : %s\n' "$data_root"
  printf '\n'
  cat <<EOF

  [CORE]
  1) Build all images
  2) Build all (no-cache)
  3) Up (default: app + nginx + db + converter)
  4) Up + convert profile
  5) Up + curator profile
  6) Up everything (all profiles)
  7) Down (stop all, keep volumes)

  [LOGS / SHELL]
  8) Logs - all (follow)
  9) Logs - pick service
 10) Shell - app
 11) Shell - converter
 12) Shell - curation-worker
 13) psql - db

  [MAINTENANCE]
 14) Backup SQLite metadata -> docs/db-backup/
 15) Reset DB (drop volume, re-init)   [confirm]

  0) Exit (or ESC)
EOF
  printf '  Choice: '
}

run_menu() {
  local choice service

  preflight

  while true; do
    show_menu
    read -r choice
    echo

    case "$choice" in
      1) build_all false ;;
      2) build_all true ;;
      3) up_target default ;;
      4) up_target convert ;;
      5) up_target curator ;;
      6) up_target all ;;
      7) down_all ;;
      8) logs_cmd ;;
      9)
        printf 'Service (app/nginx/db/converter/curation-worker): '
        read -r service
        logs_cmd "$service"
        ;;
      10) open_shell app ;;
      11) open_shell converter ;;
      12) open_shell curation-worker ;;
      13) psql_cmd ;;
      14) backup_sqlite ;;
      15)
        reset_db false
        ;;
      0|"")
        break
        ;;
      $'\e')
        break
        ;;
      *)
        printf "Invalid choice: %s\n" "$choice"
        ;;
    esac
    echo
  done
}

main() {
  case "${1:-}" in
    --up)
      up_target default
      ;;
    --up-convert)
      up_target convert
      ;;
    --up-curator)
      up_target curator
      ;;
    --up-all)
      up_target all
      ;;
    --down)
      down_all
      ;;
    --build)
      build_all false
      ;;
    --build-nocache)
      build_all true
      ;;
    --logs)
      logs_cmd "${2:-}"
      ;;
    --shell)
      [[ $# -ge 2 ]] || die "--shell requires a service name"
      open_shell "$2"
      ;;
    --psql)
      psql_cmd
      ;;
    --backup-sqlite)
      backup_sqlite
      ;;
    --reset-db)
      if [[ $# -eq 1 ]]; then
        reset_db false
      elif [[ $# -eq 2 && "${2:-}" == "--yes" ]]; then
        reset_db true
      else
        die "--reset-db accepts only an optional --yes"
      fi
      ;;
    --help|-h)
      usage
      ;;
    "")
      run_menu
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
