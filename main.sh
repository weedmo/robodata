#!/bin/bash
set -euo pipefail

# =============================================================================
# main.sh — Curation Tools Docker Launcher
#
# Interactive menu:
#   1) UI: Docker build
#   2) UI: Docker no-cache build
#   3) Up (UI + Converter, detached)
#   4) Down (UI + Converter)
#   5) UI: Logs (follow)
#   6) UI: Enter app shell
#   7) Converter: Build (no-cache) + Run (foreground)
#   8) Converter: Stop
#   9) Stop all (UI + Converter)
# =============================================================================

# === Config ===
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

UI_COMPOSE_FILE="$SCRIPT_DIR/docker/ui/docker-compose.yml"
UI_PROJECT_NAME="curation-ui"
UI_APP_SERVICE="app"
UI_NGINX_SERVICE="nginx"

CONVERTER_COMPOSE_FILE="$SCRIPT_DIR/docker/converter/docker-compose.yml"
CONVERTER_PROJECT_NAME="convert-server"
CONVERTER_SERVICE="convert-server"
CONVERTER_CONTAINER_NAME="convert-server"

# UI environment (overridable via shell env before running)
export CURATION_DATA_ROOT="${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}"
export CURATION_UI_PORT="${CURATION_UI_PORT:-18080}"

export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

# === Helper functions ===
timer_start() { date +%s.%N; }
timer_elapsed() { echo "$(echo "$(date +%s.%N) - $1" | bc)"; }
fmt_duration() {
  local secs=${1%.*}
  printf '%dh %dm %ds' $((secs/3600)) $((secs%3600/60)) $((secs%60))
}
log() { echo "[$(date '+%H:%M:%S')] $*"; }

ui_compose()        { docker compose -p "$UI_PROJECT_NAME" -f "$UI_COMPOSE_FILE" "$@"; }
converter_compose() { docker compose -p "$CONVERTER_PROJECT_NAME" -f "$CONVERTER_COMPOSE_FILE" "$@"; }

# === Pre-flight ===
preflight() {
  command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found"; return 1; }
  command -v bc     >/dev/null 2>&1 || { echo "ERROR: bc not found";     return 1; }
  docker compose version >/dev/null 2>&1 || { echo "ERROR: 'docker compose' plugin not available"; return 1; }
  [[ -f "$UI_COMPOSE_FILE" ]]        || { echo "ERROR: missing $UI_COMPOSE_FILE";        return 1; }
  [[ -f "$CONVERTER_COMPOSE_FILE" ]] || { echo "ERROR: missing $CONVERTER_COMPOSE_FILE"; return 1; }
  return 0
}

# === Status helpers ===
ui_is_running() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qE "^${UI_PROJECT_NAME}[-_](${UI_APP_SERVICE}|${UI_NGINX_SERVICE})"
}

converter_is_running() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONVERTER_CONTAINER_NAME}"
}

# Remove stale stopped container pinned to our container_name.
# Safe: only removes if container exists AND is not currently running.
# Addresses leftovers from prior `compose run --rm` that didn't clean up on interrupt.
remove_stale_converter_container() {
  if converter_is_running; then return 0; fi
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${CONVERTER_CONTAINER_NAME}$"; then
    log "Removing stale stopped '${CONVERTER_CONTAINER_NAME}' container..."
    docker rm "$CONVERTER_CONTAINER_NAME" >/dev/null 2>&1 || return 1
  fi
}

# =============================================================================
# 1) UI Build
# =============================================================================
do_ui_build() {
  preflight || return 1
  log "Building UI images..."
  local start
  start=$(timer_start)
  ui_compose build
  log "UI build complete: $(fmt_duration "$(timer_elapsed "$start")")"
}

# =============================================================================
# 2) UI No-Cache Build
# =============================================================================
do_ui_no_cache_build() {
  preflight || return 1
  log "Building UI images (no-cache)..."
  local start
  start=$(timer_start)
  ui_compose build --no-cache
  log "UI no-cache build complete: $(fmt_duration "$(timer_elapsed "$start")")"
}

# =============================================================================
# UI Up (detached, UI-only; used by --ui-up CLI flag)
# =============================================================================
do_ui_up() {
  preflight || return 1
  log "Starting UI stack..."
  log "  CURATION_DATA_ROOT=$CURATION_DATA_ROOT"
  log "  CURATION_UI_PORT=$CURATION_UI_PORT"

  [[ -d "$CURATION_DATA_ROOT" ]] || log "WARN: data root '$CURATION_DATA_ROOT' not mounted on host"

  ui_compose up -d

  log "UI is up. Open: http://localhost:${CURATION_UI_PORT}/"
  log "Tail logs via menu option 5."
}

# =============================================================================
# UI Down (UI-only; used by --ui-down CLI flag)
# =============================================================================
do_ui_down() {
  preflight || return 1
  log "Stopping UI stack..."
  ui_compose down
  log "UI stopped."
}

# =============================================================================
# 3) Up (UI + Converter, detached)
# =============================================================================
do_all_up() {
  preflight || return 1
  log "Starting UI stack..."
  log "  CURATION_DATA_ROOT=$CURATION_DATA_ROOT"
  log "  CURATION_UI_PORT=$CURATION_UI_PORT"

  [[ -d "$CURATION_DATA_ROOT" ]] || log "WARN: data root '$CURATION_DATA_ROOT' not mounted on host"

  ui_compose up -d
  log "UI is up. Open: http://localhost:${CURATION_UI_PORT}/"

  log "Starting converter (detached; builds image on first run)..."
  remove_stale_converter_container || { log "ERROR: could not remove stale '${CONVERTER_CONTAINER_NAME}'"; return 1; }
  converter_compose up -d "$CONVERTER_SERVICE"
  log "Converter is running as '${CONVERTER_CONTAINER_NAME}'."
  log "Tail UI logs via menu 5; converter logs: docker logs -f ${CONVERTER_CONTAINER_NAME}"
}

# =============================================================================
# 4) Down (UI + Converter)
# =============================================================================
do_all_down() {
  preflight || return 1
  log "Stopping UI and converter..."
  ui_compose down 2>/dev/null || true
  converter_compose down 2>/dev/null || true
  log "All stacks stopped."
}

# =============================================================================
# 5) UI Logs (follow)
# =============================================================================
do_ui_logs() {
  preflight || return 1
  if ! ui_is_running; then
    log "UI stack is not running."
    return 1
  fi
  log "Following UI logs (Ctrl+C to stop tailing; containers keep running)..."
  ui_compose logs -f --tail=100 || true
}

# =============================================================================
# 6) UI Enter app shell
# =============================================================================
do_ui_enter() {
  preflight || return 1
  if ui_is_running; then
    log "Attaching bash to running $UI_APP_SERVICE..."
    ui_compose exec "$UI_APP_SERVICE" bash
  else
    log "Stack not running. Starting one-off bash in $UI_APP_SERVICE..."
    ui_compose run --rm --entrypoint bash "$UI_APP_SERVICE"
  fi
}

# =============================================================================
# 7) Converter: Build (no-cache) + Run
# =============================================================================
do_converter_run() {
  preflight || return 1

  log "Building converter image (no-cache)..."
  local start
  start=$(timer_start)
  converter_compose build --no-cache "$CONVERTER_SERVICE"
  log "Converter build complete: $(fmt_duration "$(timer_elapsed "$start")")"

  log "Running converter (foreground; Ctrl+C to stop)..."
  remove_stale_converter_container || { log "ERROR: could not remove stale '${CONVERTER_CONTAINER_NAME}'"; return 1; }
  set +e
  converter_compose run --rm "$CONVERTER_SERVICE" python3 /app/auto_converter.py
  local exit_code=$?
  set -e

  converter_compose down 2>/dev/null || true

  if [[ $exit_code -ne 0 ]]; then
    log "Converter exited with error (exit: $exit_code)"
  else
    log "Converter finished successfully."
  fi
}

# =============================================================================
# 8) Converter: Stop
# =============================================================================
do_converter_stop() {
  preflight || return 1
  log "Stopping converter..."
  converter_compose down
  log "Converter stopped."
}

# =============================================================================
# Interactive Menu
# =============================================================================
show_menu() {
  local ui_status conv_status
  ui_is_running        && ui_status="running"   || ui_status="stopped"
  converter_is_running && conv_status="running" || conv_status="stopped"

  echo ""
  echo "============================================"
  echo "  Curation Tools — Docker Launcher"
  echo "  UI: $ui_status | Converter: $conv_status"
  echo "  UI port: $CURATION_UI_PORT | Data: $CURATION_DATA_ROOT"
  echo "============================================"
  echo "  1) UI: Build"
  echo "  2) UI: Build (no-cache)"
  echo "  3) Up (UI + Converter, detached) → http://localhost:${CURATION_UI_PORT}/"
  echo "  4) Down (UI + Converter)"
  echo "  5) UI: Logs (follow)"
  echo "  6) UI: Enter app shell"
  echo "  7) Converter: Build (no-cache) + Run (foreground)"
  echo "  8) Converter: Stop"
  echo "  9) Stop all (UI + Converter)"
  echo "  0) Exit (or ESC)"
  echo "--------------------------------------------"
  echo -n "  Choice: "
}

# === Non-interactive entry points ===
case "${1:-}" in
  --up-all)        do_all_up;        exit $? ;;
  --down-all)      do_all_down;      exit $? ;;
  --ui-up)         do_ui_up;         exit $? ;;
  --ui-down)       do_ui_down;       exit $? ;;
  --ui-build)      do_ui_build;      exit $? ;;
  --ui-nc-build)   do_ui_no_cache_build; exit $? ;;
  --ui-logs)       do_ui_logs;       exit $? ;;
  --ui-shell)      do_ui_enter;      exit $? ;;
  --converter)     do_converter_run; exit $? ;;
  "")              ;;  # fall through to interactive menu
  *)               echo "Unknown flag: $1"; exit 2 ;;
esac

# === Main loop ===
while true; do
  show_menu
  read -rsn1 c

  # ESC
  if [[ "$c" == $'\e' ]]; then
    echo -e "\n[ESC] Exit"
    break
  fi

  echo ""

  case "$c" in
    1) do_ui_build ;;
    2) do_ui_no_cache_build ;;
    3) do_all_up ;;
    4) do_all_down ;;
    5) do_ui_logs ;;
    6) do_ui_enter ;;
    7) do_converter_run ;;
    8) do_converter_stop ;;
    9) do_all_down ;;
    0)
      echo "Exit"
      break
      ;;
    *)
      echo "Invalid choice: '$c'"
      ;;
  esac

  echo ""
done
