#!/usr/bin/env bash
# Fails non-zero if any host-mode control artifact is still referenced.
set -uo pipefail

errs=0
check() {
  local pattern="$1" label="$2"
  if grep -RnE "$pattern" backend/ docker/ 2>/dev/null; then
    echo "::error:: $label still present"; errs=$((errs+1))
  fi
}
check 'CURATION_CONVERTER_CONTROL_MODE' 'control mode env'
check 'CONTROL_MODE_(AUTO|DOCKER|HOST)' 'control mode constants'
check 'convert_runtime\.json|convert_stop\.flag|convert_requests\.json|convert_events\.jsonl' 'NAS signal files'
check 'read_host_control_info|request_host_stop|enqueue_task_start_request|HostControlInfo|HOST_RUNTIME_FILE|HOST_REQUEST_FILE|HOST_EVENTS_FILE|HOST_STOP_FLAG' 'host control functions'
exit "$errs"
