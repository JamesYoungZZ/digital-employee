#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${ROOT_DIR}/.local/runtime"
PID_FILE="${RUNTIME_DIR}/web.pid"
LOG_FILE="${RUNTIME_DIR}/web.log"
HOST="${DE_WEB_HOST:-127.0.0.1}"
PORT="${DE_WEB_PORT:-8020}"

mkdir -p "${RUNTIME_DIR}"

is_running() {
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}")"
    if kill -0 "${pid}" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

case "${1:-status}" in
  start)
    if is_running; then
      echo "Digital Employee Web Control Plane already running (PID $(cat "${PID_FILE}")) at http://${HOST}:${PORT}"
      exit 0
    fi
    nohup "${ROOT_DIR}/.venv/bin/de" serve --host "${HOST}" --port "${PORT}" >"${LOG_FILE}" 2>&1 &
    echo $! >"${PID_FILE}"
    for _ in {1..20}; do
      if curl -fsS "http://${HOST}:${PORT}/api/health" >/dev/null 2>&1; then
        echo "Started Digital Employee Control Plane (PID $(cat "${PID_FILE}")) -> http://${HOST}:${PORT}"
        exit 0
      fi
      sleep 0.3
    done
    echo "Service failed health check; tailing log:" >&2
    tail -n 30 "${LOG_FILE}" >&2
    exit 1
    ;;
  stop)
    if is_running; then
      old_pid="$(cat "${PID_FILE}")"
      kill "${old_pid}" 2>/dev/null || true
      for _ in {1..20}; do
        if ! kill -0 "${old_pid}" 2>/dev/null; then
          break
        fi
        sleep 0.2
      done
      rm -f "${PID_FILE}"
      echo "Stopped Digital Employee Control Plane."
    else
      rm -f "${PID_FILE}"
      echo "Digital Employee Control Plane is not running."
    fi
    ;;
  status)
    if is_running; then
      echo "RUNNING (PID $(cat "${PID_FILE}")) -> http://${HOST}:${PORT}"
      curl -fsS "http://${HOST}:${PORT}/api/health" && echo
    else
      echo "STOPPED"
    fi
    ;;
  logs)
    tail -n 60 "${LOG_FILE}"
    ;;
  *)
    echo "Usage: $0 {start|stop|status|logs}" >&2
    exit 1
    ;;
esac
