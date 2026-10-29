#!/bin/bash
# User-owned pool: no Docker, Homebrew, sudo, or launchd required.
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
python="$repo/backend/ocr-worker/.venv/bin/python"
worker="$repo/backend/ocr-worker/worker.py"
logs="$repo/.data/logs"
mkdir -p "$logs"
cd "$repo"

running() {
    [ -f "$1" ] || return 1
    pid="$(cat "$1")"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null || return 1
    case "$(ps -p "$pid" -o command=)" in *"$worker"*) return 0 ;; *) return 1 ;; esac
}

stop_file() {
    if running "$1"; then
        kill -TERM "$pid"
        echo "Finishing current job, then stopping PID $pid ($(basename "$1"))"
    fi
}

lock_pool() {
    if ! mkdir "$logs/mac-ocr-launch.lock" 2>/dev/null; then
        echo "Another pool command is running. If a previous launcher was killed, remove the empty .data/logs/mac-ocr-launch.lock directory." >&2
        exit 1
    fi
    trap 'rmdir "$logs/mac-ocr-launch.lock"' EXIT
}

case "${1:-status}" in
    status)
        total=0
        for file in "$logs"/mac-ocr-*.pid "$logs/mac-ocr.pid"; do
            if running "$file"; then
                total=$((total + 1))
                echo "Mac OCR worker running: PID $pid ($(basename "$file"))"
            fi
        done
        echo "Running Mac OCR workers: $total"
        echo "Logs: $logs/mac-ocr-<number>.log (legacy: mac-ocr.log)"
        ;;
    stop)
        lock_pool
        for file in "$logs"/mac-ocr-*.pid "$logs/mac-ocr.pid"; do stop_file "$file"; done
        echo "Stop requested for all managed Mac OCR workers. Check status before restarting."
        ;;
    start)
        count="${2:-${OCR_WORKERS:-1}}"
        case "$count" in ''|*[!0-9]*|0|0*) echo "Worker count must be an integer from 1 to 32" >&2; exit 2 ;; esac
        if [ "${#count}" -gt 2 ] || [ "$count" -gt 32 ]; then echo "Worker count must be an integer from 1 to 32" >&2; exit 2; fi
        lock_pool
        if running "$logs/mac-ocr.pid"; then
            echo "Legacy worker PID $pid is running. Run stop, wait for it to exit, then start the pool." >&2
            exit 1
        fi
        bash "$0" check
        for file in "$logs"/mac-ocr-*.pid; do
            [ -f "$file" ] || continue
            slot="$(basename "$file" .pid)"
            slot="${slot#mac-ocr-}"
            case "$slot" in ''|*[!0-9]*) continue ;; esac
            if [ "$slot" -gt "$count" ]; then stop_file "$file"; fi
        done
        for ((slot=1; slot<=count; slot++)); do
            pidfile="$logs/mac-ocr-$slot.pid"
            logfile="$logs/mac-ocr-$slot.log"
            if running "$pidfile"; then
                echo "Mac OCR worker $slot already running: PID $pid"
                continue
            fi
            nohup bash "$0" run >>"$logfile" 2>&1 </dev/null &
            echo "$!" > "$pidfile"
            echo "Mac OCR worker $slot starting: PID $(cat "$pidfile"); log: $logfile"
            # Stagger model loading to reduce simultaneous startup pressure.
            if [ "$slot" -lt "$count" ]; then sleep 2; fi
        done
        echo "Requested $count Mac OCR workers; verify readiness in each log. Each worker loads its own models."
        ;;
    check|run)
        export OCR_DEVICE="${OCR_DEVICE:-mps}"
        export OCR_IMAGE_API_URL="${OCR_IMAGE_API_URL:-http://127.0.0.1:15125}"
        export RABBITMQ_HOST="${RABBITMQ_HOST:-127.0.0.1}"
        export RABBITMQ_PORT="${RABBITMQ_PORT:-15673}"
        export YOLO_AUTOINSTALL=false
        # Credentials retain worker.py defaults, or may be supplied as environment variables.
        if [ "${1:-}" = check ]; then
            "$python" - <<'PY'
import os, socket
from urllib.request import ProxyHandler, build_opener
with socket.create_connection((os.environ['RABBITMQ_HOST'], int(os.environ['RABBITMQ_PORT'])), timeout=5):
    pass
with build_opener(ProxyHandler({})).open(os.environ['OCR_IMAGE_API_URL'] + '/health', timeout=10) as response:
    assert response.status == 200
print('Windows RabbitMQ tunnel reachable; Windows API healthy')
PY
        else
            exec "$python" -u "$worker"
        fi
        ;;
    logs)
        files=()
        for file in "$logs"/mac-ocr-*.log; do [ ! -f "$file" ] || files+=("$file"); done
        if [ "${#files[@]}" -eq 0 ]; then echo "No pool logs yet"; exit 0; fi
        exec tail -n 10 -F "${files[@]}"
        ;;
    *) echo "Usage: bash backend/scripts/mac-worker.sh {start [count]|stop|status|check|run|logs}" >&2; exit 2 ;;
esac
