FROM grafana/grafana:13.1.0

USER root

RUN apk add --no-cache \
        openssh-client \
        python3 \
        py3-pip \
        curl \
        bash \
    && pip3 install --no-cache-dir --break-system-packages uv

RUN grafana cli --pluginsDir "/var/lib/grafana/plugins" plugins install marcusolsson-csv-datasource 1.0.0

COPY elmgwplot.py /opt/elmgwplot/elmgwplot.py

RUN uv run --script /opt/elmgwplot/elmgwplot.py --help >/dev/null 2>&1 \
    && chmod +x /opt/elmgwplot/elmgwplot.py

COPY fetch-and-plot.sh /opt/elmgwplot/fetch-and-plot.sh
RUN chmod +x /opt/elmgwplot/fetch-and-plot.sh

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

COPY provisioning/dashboards/elm-dashboard.yml /etc/grafana/provisioning/dashboards/elm-dashboard.yml
COPY provisioning/dashboards/elm-vis-dashboard.json /var/lib/grafana/dashboards/elm-vis-dashboard.json
COPY provisioning/datasources/csv.yml /etc/grafana/provisioning/datasources/csv.yml

RUN mkdir -p /data/logs /data/plots /keys

ENV GF_SECURITY_ADMIN_PASSWORD=admin \
    GF_USERS_ALLOW_SIGN_UP=false \
    GF_PANELS_DISABLE_SANITIZE_HTML=true \
    GF_AUTH_ANONYMOUS_ENABLED=true \
    GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
    GF_PLUGINS_ENABLE_ALPHA=true \
    GF_PLUGIN_ALLOW_LOCAL_MODE=true \
    GF_PLUGINS_FORWARD_HOST_ENV_VARS="marcusolsson-csv-datasource" \
    GF_SERVER_HTTP_PORT=80 \
    PLOTS_DIR=/data/plots \
    REMOTE_HOST= \
    SSH_KEY_PATH=/keys/id

EXPOSE 80

ENTRYPOINT ["/entrypoint.sh"]
