#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

resolve_path() {
    local path="$1"
    if [[ "$path" = /* ]]; then
        printf '%s\n' "$path"
    else
        printf '%s\n' "$SCRIPT_DIR/$path"
    fi
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

PROJECT_NAME="${PROJECT_NAME:-curation-tools}"
COMPOSE_FILE="$(resolve_path "${COMPOSE_FILE:-docker/compose.yml}")"
ENV_FILE="$(resolve_path "${ENV_FILE:-docker/.env}")"
ENV_EXAMPLE_FILE="$(resolve_path "${ENV_EXAMPLE_FILE:-docker/.env.example}")"
DEFAULT_DATA_ROOT="/mnt/synology/data/data_div/2026_1"

compose() {
    docker compose --env-file "$ENV_FILE" -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
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

ensure_compose_services() {
    command -v docker >/dev/null 2>&1 || die "docker not found"
    docker compose version >/dev/null 2>&1 || die "'docker compose' plugin not available"
    [[ -f "$COMPOSE_FILE" ]] || die "Missing compose file: $COMPOSE_FILE"
    ensure_env_file

    echo "Starting Postgres via docker compose..."
    compose up -d db >/dev/null
    wait_for_db_ready
}

wait_for_db_ready() {
    local timeout_seconds="${DB_READY_TIMEOUT_SECONDS:-60}"
    local elapsed=0
    local container_id=
    local status=

    while (( elapsed < timeout_seconds )); do
        container_id="$(compose ps -q db 2>/dev/null || true)"
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

    die "Postgres service did not become ready"
}

setup_python_env() {
    if [[ ! -d ".venv" ]]; then
        echo "Setting up Python environment..."
        if ! command -v uv >/dev/null 2>&1; then
            curl -LsSf https://astral.sh/uv/install.sh | sh
            export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
        fi
        uv venv .venv
        source .venv/bin/activate
        uv pip install -e .
        echo "Python environment ready."
    else
        source .venv/bin/activate
    fi
}

setup_frontend_deps() {
    if [[ ! -d "frontend/node_modules" ]]; then
        echo "Installing frontend dependencies..."
        (cd frontend && npm install)
        echo "Frontend dependencies ready."
    fi
}

configure_host_backend_env() {
    set -a
    source "$ENV_FILE"
    set +a

    PG_BIND="${CURATION_PG_HOST_PORT:-127.0.0.1:5433}"
    if [[ "$PG_BIND" == *:* ]]; then
        PG_HOST="${PG_BIND%:*}"
        PG_PORT="${PG_BIND##*:}"
    else
        PG_HOST="127.0.0.1"
        PG_PORT="$PG_BIND"
    fi

    export CURATION_DB_URL="postgresql://${POSTGRES_USER:-curation}:${POSTGRES_PASSWORD:-dev-only-change-me}@${PG_HOST}:${PG_PORT}/${POSTGRES_DB:-curation}"
    export CURATION_DATASET_ROOT_BASE="${CURATION_DATASET_ROOT_BASE:-${CURATION_DATA_ROOT:-$DEFAULT_DATA_ROOT}}"
    export CURATION_DATASET_PATH="${CURATION_DATASET_PATH:-${CURATION_DATASET_ROOT_BASE}/lerobot}"
}

ensure_compose_services
setup_python_env
setup_frontend_deps
configure_host_backend_env

BACKEND_PORT="${BACKEND_PORT:-8001}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
export VITE_PORT="$FRONTEND_PORT"
export VITE_BACKEND_URL="${VITE_BACKEND_URL:-http://localhost:${BACKEND_PORT}}"

echo "Hybrid dev mode:"
echo "  Dataset : $CURATION_DATASET_PATH"
echo "  Backend : http://localhost:${BACKEND_PORT}"
echo "  Frontend: http://localhost:${FRONTEND_PORT}"
echo "  DB      : $CURATION_DB_URL"

uvicorn backend.main:app --host 0.0.0.0 --port "$BACKEND_PORT" &
BACKEND_PID=$!
(cd frontend && npm run dev) &
FRONTEND_PID=$!

# Let both child launch commands enter their runtimes before wait -n handles an
# immediate failure and cleanup starts terminating siblings.
sleep "${HOST_LAUNCH_SETTLE_SECONDS:-0.1}"

cleanup() {
    echo
    echo "Shutting down host processes..."
    kill $BACKEND_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
    wait
}
trap cleanup EXIT INT TERM

set +e
wait -n "$BACKEND_PID" "$FRONTEND_PID"
EXIT_STATUS=$?
set -e

exit "$EXIT_STATUS"
