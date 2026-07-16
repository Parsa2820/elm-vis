#!/usr/bin/env bash
set -euo pipefail

PLOTS_DIR="${PLOTS_DIR:-/data/plots}"
REMOTE_HOST="${REMOTE_HOST:-}"
SSH_KEY_PATH="${SSH_KEY_PATH:-/keys/id}"
LOG_DIR="/data/logs"

export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="/tmp"

if [ -z "$REMOTE_HOST" ]; then
    echo "[fetch-and-plot] REMOTE_HOST is not set; skipping fetch." >&2
    exit 0
fi

if [ ! -f "$SSH_KEY_PATH" ]; then
    echo "[fetch-and-plot] SSH key $SSH_KEY_PATH not found; skipping." >&2
    exit 1
fi

chmod 600 "$SSH_KEY_PATH" 2>/dev/null || true

mkdir -p "$LOG_DIR" "$PLOTS_DIR"

echo "[fetch-and-plot] fetching tio* from $REMOTE_HOST ..."
scp -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    -i "$SSH_KEY_PATH" \
    "$REMOTE_HOST:tio*" "$LOG_DIR/" || {
        echo "[fetch-and-plot] scp failed." >&2
        exit 1
    }

shopt -s nullglob
logs=( "$LOG_DIR"/tio_ttyAMA0_* )
shopt -u nullglob

if [ ${#logs[@]} -eq 0 ]; then
    echo "[fetch-and-plot] no tio_ttyAMA0_* logs in $LOG_DIR; nothing to plot." >&2
    exit 0
fi

echo "[fetch-and-plot] generating plots for ${#logs[@]} log(s) ..."
uv run --script /opt/elmgwplot/elmgwplot.py "${logs[@]}" --outdir "$PLOTS_DIR"

# Freshness table for the dashboard: when this fetch ran, plus the last time
# each node was heard from. elmgwplot.py reports this as "last_heard_ago_h" =
# hours between the node's last packet and the end of the log.
FETCHED_AT="$(TZ='America/Toronto' date +"%Y-%m-%dT%H:%M:%S%z")"
{
    echo "node,last_fetch,last_heard_ago"
    echo "(data fetch),$FETCHED_AT,"
    if [ -f "$PLOTS_DIR/H_packet_nodes.csv" ]; then
        # elmgwplot.py's csv.writer uses CRLF line endings; strip the \r
        # before splitting fields or it ends up stuck to the last column.
        # Column 7 is last_heard_ago_h (hours since the node's last packet,
        # measured against the end of the log); convert "<float> h" into
        # "Nh Nm" for a friendlier read in the dashboard table.
        tail -n +2 "$PLOTS_DIR/H_packet_nodes.csv" | tr -d '\r' \
            | awk -F',' -v OFS=',' '{ h=int($7); m=int(($7-h)*60); print $2, "", h "h " m "m" }'
    fi
} > "$PLOTS_DIR/J_freshness.csv"

echo "[fetch-and-plot] done."
