#!/usr/bin/env bash
set -e

export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="/tmp"
ENV_FILE="/tmp/cron.env"

env | grep -E '^(PATH|HOME|PLOTS_DIR|REMOTE_HOST|SSH_KEY_PATH|GF_|LANG|LC_)' > "$ENV_FILE"

cat > /tmp/elm-cron <<EOF
5,35 * * * * . $ENV_FILE && /opt/elmgwplot/fetch-and-plot.sh >> /var/log/fetch-and-plot.log 2>&1
EOF

crontab /tmp/elm-cron

/opt/elmgwplot/fetch-and-plot.sh || true

crond -l 8 >/var/log/crond.log 2>&1 &

exec grafana server \
    --homepath=/usr/share/grafana \
    --config=/etc/grafana/grafana.ini \
    --packaging=container \
    cfg:default.paths.logs=/var/log/grafana \
    cfg:default.paths.data=/var/lib/grafana \
    cfg:default.paths.plugins=/var/lib/grafana/plugins
