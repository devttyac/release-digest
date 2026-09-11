#!/bin/sh
# Two modes:
#   scheduler  (default) long-running; fires the digest monthly. Suits Dockge,
#              compose stacks, anything that expects a service to stay up.
#   once       run the digest now and exit. Suits a systemd timer, cron, or a
#              manual `docker run`.
set -eu

MODE="${1:-scheduler}"

if [ "$MODE" = "scheduler" ]; then
    exec python /app/scheduler.py
fi

if [ "$MODE" != "once" ]; then
    echo "unknown mode: '$MODE' (expected 'scheduler' or 'once')" >&2
    exit 64
fi

DATA_FILE=/tmp/release-digest-data.json

# Optional knobs, all unset by default:
#   DIGEST_MONTH  YYYY-MM to target a specific month instead of the current one
#   ARCHIVE_DIR   write the rendered HTML + raw JSON here (mount a volume)
#   DRY_RUN       any non-empty value builds the message without sending
MONTH="${DIGEST_MONTH:-}"
ARCHIVE="${ARCHIVE_DIR:-}"
DRY="${DRY_RUN:-}"

# Note: plain `[ -n "$X" ] && set -- ...` would abort the whole script under
# `set -e` whenever the test is false, because the && chain then exits non-zero.
echo "==> fetch"
if [ -n "$MONTH" ]; then
    python fetch_releases.py --out "$DATA_FILE" "$MONTH"
else
    python fetch_releases.py --out "$DATA_FILE"
fi

echo "==> send"
set -- python send_digest.py "$DATA_FILE"
if [ -n "$ARCHIVE" ]; then
    set -- "$@" --archive-dir "$ARCHIVE"
fi
if [ -n "$DRY" ]; then
    set -- "$@" --dry-run
fi
exec "$@"
