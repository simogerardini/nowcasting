#!/usr/bin/env bash
# Single-container entrypoint (Hugging Face Spaces): worker in background (auto-restart) + API in foreground.
set -u
(
  while true; do
    python -m worker.scheduler
    echo "worker exited ($?), restarting in 15s" >&2
    sleep 15
  done
) &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-7860}" --proxy-headers --forwarded-allow-ips "*"
