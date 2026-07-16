# elm-vis

ELM Network Telemetry — a self-contained Docker image that fetches an ELM
gateway's serial log over SSH, turns it into a set of plots + CSVs with
`elmgwplot.py`, and serves them in a pre-provisioned Grafana dashboard.

The container is built on `grafana/grafana:13.1.0`, adds the
`marcusolsson-csv-datasource` plugin so the dashboard can read the generated
CSVs directly, installs `uv` to run the Python plotter as a standalone
script, and wires up a cron job that re-fetches and re-plots every 30 minutes
(ports 5 and 35 past the hour). Grafana then refreshes its panels on a 5-minute
cadence from the same CSV directory the plotter writes to.

## Repository layout

```
elmgwplot.py            ELM gateway log parser + plotter (uv inline-script)
gen_dashboard.py        Generates provisioning/dashboards/elm-vis-dashboard.json
fetch-and-plot.sh       scp's tio* logs from the gateway, runs elmgwplot.py,
                        writes J_freshness.csv
entrypoint.sh           Installs the cron job, runs fetch-and-plot once, then
                        starts crond + grafana server
Dockerfile              Builds the whole image (base = grafana/grafana:13.1.0)
provisioning/
  dashboards/
    elm-dashboard.yml              Grafana file-provisioning provider config
    elm-vis-dashboard.json        Generated dashboard (run gen_dashboard.py)
  datasources/
    csv.yml                       elm-csv  -> /data/plots  (default)
                                   elm-logs -> /data/logs
```

Runtime paths inside the container (created by the image, not committed):

```
/data/plots/             CSVs + PNGs written by elmgwplot.py (CSV datasource root)
/data/logs/             tio_ttyAMA0_* logs fetched via scp
/keys/id                Your SSH private key (mount your own at /keys/id)
node_locations.csv      Optional GPS coordinates for the topology geomap panel
                        (drop in /data/plots/ — appears on the next refresh)
tioups.log              Gateway host health CSV (Battery/CPU/RAM/Storage)
                        (fetched from REMOTE_HOST into /data/logs/ — read by
                        the Gateway Info panel via the elm-logs datasource)
```

## What `elmgwplot.py` produces

Everything is derived from the gateway's own log lines (spec section 18).
Each figure is emitted as both a PNG and one or more CSVs, so every plot can
be rebuilt from the CSV alone (e.g. in pgfplots).

| ID | Figure                          | CSV(s)                                   |
|----|---------------------------------|------------------------------------------|
| A  | PDR by node                     | `A_pdr_by_node.csv`                      |
| B  | PDR by hop count                | `B_pdr_by_hop.csv`                       |
| C  | End-to-end delay by node        | `C_delay_by_node_{stats,outliers,ecdf}.csv`, `C_delay_samples.csv` |
| D  | End-to-end delay by hop count   | `D_delay_by_hop_{stats,outliers,ecdf}.csv` |
| E  | Logical topology                | `E_topology_{edges,nodes}.csv`           |
| F  | Battery state of charge         | `F_battery.csv`, `F_battery_nodes.csv`   |
| G  | Gateway-observable event timeline | `G_events.csv`, `G_event_{nodes,kinds}.csv` |
| H  | Packet arrivals at the gateway  | `H_packets.csv`, `H_packet_nodes.csv`    |
| I  | Reported temperature            | `I_temperature.csv`, `I_temperature_nodes.csv` |
| J  | Data freshness (fetch-and-plot) | `J_freshness.csv`                        |

`J_freshness.csv` is written by `fetch-and-plot.sh`, not `elmgwplot.py`: the
first row is the wall-clock time of the last successful fetch, and the
remaining rows reuse the per-node packet stats to show when each node was last
heard from.

### PDR

By default the plotter uses the gateway's own `est P%` (spec 18.1):
`rcvd / (max_id - min_id + 1)`. With `--generatedcsv` (a `node,packets` file
holding each node's persisted EEPROM packet count) the authoritative PDR
`rcvd / generated` is computed and plotted alongside it.

### Duplicates

A `packet_id` delivered twice is a lost-ack retransmit: it counts once in the
PDR, once per arrival in figure H (where it is marked).

### Hops

Every `GATEWAY data` delivery is followed by that packet's own `TOPO` line,
so each delay sample and each delivered `packet_id` is filed under the hop
count it actually travelled. Lost `packet_id`s (gaps in the id sequence)
inherit the hop count of the nearest delivered id.

## Building the image

```bash
docker build -t elm-vis .
```

Run `gen_dashboard.py` first if you need to regenerate the dashboard JSON:

```bash
python3 gen_dashboard.py
# -> provisioning/dashboards/elm-vis-dashboard.json
```

## Running the container

```bash
docker run -d --name elm-vis -p 8080:80 \
  -v ~/.ssh/elm_gw_key:/keys/id \
  -e REMOTE_HOST=user@gateway-host \
  elm-vis
```

Environment variables (defaults shown):

| Variable              | Default     | Meaning                                              |
|-----------------------|-------------|------------------------------------------------------|
| `REMOTE_HOST`         | *(empty)*   | `user@host` passed to `scp` for `tio*` logs (fetched from the remote user's home directory). If empty, the fetch step is skipped (useful for running with logs already in `/data/logs`). |
| `SSH_KEY_PATH`        | `/keys/id`  | Private key used by `scp`; must be mounted into the container. |
| `PLOTS_DIR`           | `/data/plots`| Where `elmgwplot.py` writes CSVs + PNGs (also the CSV datasource root). |
| `GF_SECURITY_ADMIN_PASSWORD` | `admin` | Grafana admin password. |
| `GF_AUTH_ANONYMOUS_ENABLED`  | `true`  | Anonymous viewer access enabled. |

The dashboard is served on port `80` inside the container, timezone
`America/Toronto`, auto-refreshing every 5 minutes. Open
`http://localhost:8080` (anonymous viewer) or log in as `admin` / `admin`
for edit access.

## Dashboard panels

The generated `elm-vis-dashboard.json` provisions the following panels,
grouped into rows (collapsed by default):

1. **Delivery Ratio** —
   A. PDR by node (barchart),
   B. PDR by hop count (barchart)
2. **End-to-end Delay** —
   C. Delay by node (mean, barchart),
   D. Delay by hop count (mean, barchart)
3. **Topology** —
   E. Logical topology (geomap Network layer, reads `node_locations.csv`
      for coordinates and `E_topology_edges.csv` for edges). Drop real GPS
      data into `node_locations.csv` and it appears on the next refresh —
      no dashboard regeneration needed.
4. **Data** —
   J. Data freshness (table),
   I. Reported temperature over time (timeseries),
   F. Reported battery percentage over time (timeseries)
5. **Gateway Info** —
   GW. Gateway health gauges (latest Battery/CPU/RAM/Storage from `tioups.log`)

### CSV datasource notes

The dashboard targets use the `marcusolsson-csv-datasource` plugin's
`csvFile` query type. Each query declares an explicit `schema` (column name +
type) so that columns without a schema entry are silently dropped — a
barchart panel then plots only the category field plus the single value
field it should show. Unit formatting is scoped per-field via
`fieldConfig.overrides` rather than as a blanket default, since defaults also
apply to the category field.

## Running `elmgwplot.py` standalone

```bash
uv run elmgwplot.py tio_ttyAMA0_2026-07-08T17:59:57.log
uv run elmgwplot.py today.log --outdir plots --format png --dpi 150
uv run elmgwplot.py today.log --generatedcsv node_counts.csv
```

Options:

| Flag             | Default                  | Description                                  |
|------------------|--------------------------|----------------------------------------------|
| `log`            | *(required)*             | Gateway serial log to parse.                 |
| `--outdir`       | `<log dir>/plots`        | Output directory for CSVs and PNGs.          |
| `--generatedcsv` | *(none)*                 | `node,packets` CSV of persisted EEPROM counts; enables authoritative PDR. |
| `--format`       | `png`                    | `png`, `pdf`, or `svg`.                      |
| `--dpi`          | `150`                    | PNG/SVG rasterisation DPI.                   |

The ELM gateway log is produced by the firmware's serial console capture via
`tio` on the gateway host over `/dev/ttyAMA0`; lines that match the
`[ELM] GATEWAY data from …` / `[ELM] TOPO …` / `[ELM] PDR …` formats are
parsed into deliveries, topology reports, PDR estimates, and events
(joins, rejects, slot reassignments, child expirations, reboots, etc.).

## Dependencies

- `elmgwplot.py`: Python >=3.10, `matplotlib>=3.8`, `networkx>=3.2`
  (installed automatically by `uv` via inline script metadata — no
  `pip install` needed).
- `gen_dashboard.py`: Python 3 stdlib (`json` only).
- Container: `grafana/grafana:13.1.0`, `marcusolsson-csv-datasource` 1.0.0,
  `uv`, `openssh-client`, `python3`, `curl`, `bash`.

## Log line grammar (parser reference)

```
[YYYY-MM-DDTHH:MM:SS.mmm] [ELM] GATEWAY data from node 0xNN
      (origin 0xNN, id N, signal -N dBm) | end-to-end delay N s |
      temp N.N C | humidity N.N % | battery N %
[ELM] TOPO origin 0xNN | parents N: 0xNN 0xNN 0xNN
      (first is preferred, 0x00 unused) | rank N hops N | ch N
[ELM] PDR origin 0xNN | rcvd N | id range a..b | est P%
[ELM] GATEWAY clock HH:MM:SS.ss (up NhNmNs) | data rcvd N (acked N) |
      keepalives rcvd N (acked N)
[ELM] PARENT got join request | accepting | admitting | re-admitting ...
[ELM] freed inactive child 0xNN (silent N ms >= N ms) | DC slot N reclaimed
[ELM] data buffer full (N), not acking relayed data from 0xNN
[ELM] NODE sent/acked slot reassignment to/from child 0xNN (-> DC slot N)
[ELM] NODE sent data forward failure to child 0xNN (origin 0xNN, id N)
[ELM] WARNING: radio send failed or timed out
[ELM] Starting up as <role>
```

Lines that don't match any of the above are ignored. A `GATEWAY data` line
must be immediately followed by its `TOPO` line; any intervening line
breaks the adjacency and the delivery's hop/rank/parent info is left unset.
