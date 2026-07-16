#!/usr/bin/env python3
"""Generates provisioning/dashboards/elm-vis-dashboard.json.

The marcusolsson-csv-datasource plugin requires:
- the query field to be named "path" (not "file")
- a "schema" array declaring the type of every column that should be
  kept; with ignoreUnknown=true, any column without a schema entry is
  silently dropped, producing an empty frame.

Note on barchart panels: Grafana's barchart panel plots every numeric
field present in the frame as its own series -- the "yField" option
does not restrict this. To show a single value per category, the
query's schema must only declare the category field plus the one
value field.

Note on units: fieldConfig.defaults applies to every field in the
frame, including the category field (e.g. "node" or "hops"). Unit
formatting must be scoped to the value field only via a fieldConfig
override, not set as a blanket default.
"""
import json

DS_TYPE = "marcusolsson-csv-datasource"
DS_UID = "elm-csv"
DS_UID_LOGS = "elm-logs"


def csv_query(refid, filename, schema, ignore_unknown=True, uid=DS_UID):
    return {
        "refId": refid,
        "datasource": {"type": DS_TYPE, "uid": uid},
        "queryType": "csvFile",
        "path": filename,
        "header": True,
        "delimiter": ",",
        "skipRows": 0,
        "decimalSeparator": ".",
        "ignoreUnknown": ignore_unknown,
        "schema": schema,
    }


def fnum(name):
    return {"name": name, "type": "number"}


def fstr(name):
    return {"name": name, "type": "string"}


def unit_override(field_name, unit, min_=None, max_=None):
    props = [{"id": "unit", "value": unit}]
    if min_ is not None:
        props.append({"id": "min", "value": min_})
    if max_ is not None:
        props.append({"id": "max", "value": max_})
    return {"matcher": {"id": "byName", "options": field_name}, "properties": props}


def convert_field_type(target_field, destination_type, date_format=None):
    """The convertFieldType transform's actual options shape is
    {"conversions": [{"targetField", "destinationType", "dateFormat", ...}]}
    -- NOT {"fields": {name: {"convertTo": ...}}}. The latter silently
    no-ops (confirmed by reading the transformer's own minified source:
    it only ever reads options.conversions, matching each entry's
    targetField against field names)."""
    conversion = {"targetField": target_field, "destinationType": destination_type}
    if date_format is not None:
        conversion["dateFormat"] = date_format
    return {"id": "convertFieldType", "options": {"conversions": [conversion], "fields": {}}}


def panel(pid, title, ptype, gridpos, targets, options=None, fieldconfig=None,
          transformations=None, overrides=None, time=None, uid=DS_UID):
    p = {
        "id": pid, "type": ptype, "title": title, "gridPos": gridpos,
        "datasource": {"type": DS_TYPE, "uid": uid},
        "targets": targets,
        "options": options or {},
        "fieldConfig": {"defaults": (fieldconfig or {}), "overrides": overrides or []},
        "transformations": transformations or [],
        # Every panel stamps the Grafana version it was authored against. This
        # is mandatory for XY-chart panel 18: its migration handler rewrites
        # any panel whose pluginVersion is empty as a legacy scatter config,
        # wrapping the new-format {matcher:{...}} series inside a byName
        # matcher's options. The decorated matcher then matches no field, the
        # panel iterates zero series, the prepConfig "xySeries.length === 0"
        # branch fires, and XY-charts render "No data". Stamping >= 11.1
        # sends the handler down its "return panel.options" fast path so the
        # series config survives untouched.
        "pluginVersion": "13.1.0",
    }
    if time is not None:
        p["time"] = time
    return p



def row(pid, title, y):
    return {"id": pid, "type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "collapsed": False, "panels": [], "datasource": None}


def fold_rows(items):
    """Return the panel list with every row collapsed-by-default except rows
    named in OPEN_ROWS. Grafana represents a collapsed row by moving its
    member panels *inside* the row's own "panels" array; an open row
    instead keeps them as top-level siblings of the row object. So this
    regroups the flat list based on each row's collapsed state."""
    # Rows whose contents stay visible on dashboard load.
    OPEN_ROWS = {"Data"}
    out = []
    current = None
    collapse_current = True
    for it in items:
        if it["type"] == "row":
            collapse_current = it["title"] not in OPEN_ROWS
            it["collapsed"] = collapse_current
            it["panels"] = []
            current = it
            out.append(it)
        elif current is not None and collapse_current:
            # Collapsed row: the member panels live inside the row's
            # "panels" array so Grafana hides them until the row expands.
            current["panels"].append(it)
        else:
            # Open row (or no current row): the panel stays a top-level
            # sibling, rendered on dashboard load.
            out.append(it)
    return out


panels = []

# Row 1: Delivery Ratio
panels.append(row(1, "Delivery Ratio", 0))

bar_opts = {"orientation": "auto", "barWidth": 0.7, "showValue": "auto",
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "single", "sort": "none"}}

panels.append(panel(2, "A. Packet delivery ratio by node", "barchart",
    {"h": 8, "w": 24, "x": 0, "y": 1},
    [csv_query("A", "A_pdr_by_node.csv", [fstr("node"), fnum("est_pdr_pct")])],
    {**bar_opts, "xField": "node", "yField": "est_pdr_pct"},
    {},
    overrides=[unit_override("est_pdr_pct", "percent", 0, 105)]))

panels.append(panel(3, "B. Packet delivery ratio by hop count", "barchart",
    {"h": 8, "w": 24, "x": 0, "y": 9},
    [csv_query("B", "B_pdr_by_hop.csv", [fnum("hops"), fnum("pdr_pct")])],
    {**bar_opts, "xField": "hops", "yField": "pdr_pct"},
    {},
    [convert_field_type("hops", "string")],
    overrides=[unit_override("pdr_pct", "percent", 0, 105)]))

# Row 2: End-to-end Delay
panels.append(row(4, "End-to-end Delay", 17))

panels.append(panel(5, "C. End-to-end delay by node (mean)", "barchart",
    {"h": 8, "w": 12, "x": 0, "y": 18},
    [csv_query("C", "C_delay_by_node_stats.csv", [fstr("node"), fnum("mean")])],
    {**bar_opts, "xField": "node", "yField": "mean"},
    {},
    overrides=[unit_override("mean", "s", 0)]))

panels.append(panel(6, "D. End-to-end delay by hop count (mean)", "barchart",
    {"h": 8, "w": 12, "x": 12, "y": 18},
    [csv_query("D", "D_delay_by_hop_stats.csv", [fnum("hops"), fnum("mean")])],
    {**bar_opts, "xField": "hops", "yField": "mean"},
    {},
    [convert_field_type("hops", "string")],
    overrides=[unit_override("mean", "s", 0)]))

# Row 3: Topology
panels.append(row(9, "Topology", 26))

# esnet-networkmap-panel (a third-party plugin) had unfixable zoom/rendering
# problems. Switched to Grafana's own core Geomap panel with its built-in
# "Network" layer -- no plugin install needed, and it's the same map/zoom
# UI used by every other Geomap panel. The Network layer reuses the Node
# Graph data API (a nodes frame with "id", an edges frame with "source"/
# "target"/"id") plus geospatial coordinates on the nodes frame, and reads
# it live on every query -- so real GPS data dropped into node_locations.csv
# later shows up on refresh with no dashboard regeneration needed.
panels.append(panel(10, "E. Logical topology", "geomap",
    {"h": 12, "w": 24, "x": 0, "y": 27},
    [
        # Geomap's Network layer matches frames by *real* field name: the
        # nodes frame needs a field literally named "id", the edges frame
        # fields literally named "source"/"target". An Organize/rename
        # transform only sets a field's displayName, never its name, so the
        # layer can't recognise renamed frames (getGraphFrame then files the
        # edges frame under nodes, leaves edges empty, and the layer draws
        # nothing). The CSV columns therefore carry these exact names.
        csv_query("N", "node_locations.csv", [fstr("id"), fnum("lat"), fnum("lon")]),
        csv_query("E2", "E_topology_edges.csv", [fstr("source"), fstr("target")]),
    ],
    {
        "view": {"id": "coords", "lat": 43.733139, "lon": -79.447944, "zoom": 15},
        "controls": {"showZoom": True, "mouseWheelZoom": True, "showScale": False,
                     "showAttribution": True, "showMeasure": False, "showDebug": False},
        "basemap": {"type": "default", "name": "Basemap", "config": {}},
        "layers": [{
            "type": "network",
            "name": "Topology",
            "location": {"mode": "coords", "latitude": "lat", "longitude": "lon"},
            "config": {"arrow": 0, "showLegend": True},
            "tooltip": True,
        }],
        "tooltip": {"mode": "details"},
    },
    ))

# Row 4: Data
panels.append(row(15, "Data", 39))

# Freshness table surfaced AT THE TOP OF THE OPEN "Data" ROW so a viewer
# opening the dashboard immediately sees how recent the underlying data is.
# J_freshness.csv is written by fetch-and-plot.sh and has one summary row
# (node="(data fetch)", last_fetch=<wall-clock grab>, last_log_line=<ISO of
# the log's last timestamped line>) followed by one row per node carrying
# that node's last-heard-from age. last_fetch comes from the shell's `date`
# and carries a numeric zone offset (-0400), while last_log_line is the naive
# ISO elmgwplot.py emits (YYYY-MM-DDTHH:mm:ss.SSS, no zone). Both are declared
# "string" up front and then converted to "time" via convertFieldType, the
# SAME pattern the temperature/battery panels use -- declaring them "time" in
# the CSV schema instead makes the datasource parse them as UTC (4h offset
# from the dashboard's Toronto display). see the convertFieldType notes on
# panel 18 for the underlying reason. last_heard_ago stays a string ("Nh Nm")
# for a friendly read and is rendered as-is in the table.
freshness_transforms = [
    convert_field_type("last_fetch", "time", date_format="YYYY-MM-DDTHH:mm:ssZZ"),
    convert_field_type("last_log_line", "time", date_format="YYYY-MM-DDTHH:mm:ss.SSS"),
]
panels.append(panel(19, "Freshness — last fetch / last log line", "table",
    {"h": 4, "w": 24, "x": 0, "y": 40},
    [csv_query("F", "J_freshness.csv", [
        fstr("node"), fstr("last_fetch"), fstr("last_log_line"), fstr("last_heard_ago")])],
    # showHeader + a tight cell padding keep all 4 columns on one row each.
    {"showHeader": True, "footer": {"show": False}},
    {},
    freshness_transforms,
    overrides=[
        # Right-align ISO time columns so timestamps line up in the header row.
        {"matcher": {"id": "byName", "options": "last_fetch"},
         "properties": [{"id": "custom.align", "value": "right"},
                        {"id": "unit", "value": "time: YYYY-MM-DD HH:mm:ss"}]},
        {"matcher": {"id": "byName", "options": "last_log_line"},
         "properties": [{"id": "custom.align", "value": "right"},
                        {"id": "unit", "value": "time: YYYY-MM-DD HH:mm:ss"}]},
        {"matcher": {"id": "byName", "options": "last_heard_ago"},
         "properties": [{"id": "custom.align", "value": "right"}]},
    ],
    ))

dot_line_custom = {"drawStyle": "line", "lineWidth": 1, "pointSize": 5, "showPoints": "always",
                   "spanNulls": False}
node_split_transforms = [
    # "timestamp" comes out of the CSV as plain text; convert it to a real
    # time value so the panel gets a genuine time axis.
    convert_field_type("timestamp", "time", date_format="YYYY-MM-DDTHH:mm:ss.SSS"),
    # Splits the single long-format frame (time, node, value) into one frame
    # per distinct "node" value; the timeseries panel then renders each
    # frame as its own series with its own color and legend entry.
    {"id": "partitionByValues", "options": {"fields": ["node"], "naming": {"asLabels": True}}},
]

# J_freshness.csv is written by fetch-and-plot.sh (not elmgwplot.py): first
# row is the wall-clock time of the last successful fetch, remaining rows
# are each node's last-heard-from time as elapsed hours since that node's
# last packet to the end of the log -- reusing the per-node stats
# elmgwplot.py computes as "last_heard_ago_h" in H_packet_nodes.csv.
#
# Panel 18 is the "racing to now" matrix the dashboard reader asked for:
# nodes on the Y axis, time on the X axis (right edge == dashboard "now"),
# one dot per packet event. Each node gets its own color AND a thin
# connecting line through its packets, so a reader can trace one node's
# sequence across the matrix at a glance.
#
# Implementation notes:
#   * partitionByValues("node"): splits the single H_packets frame into one
#     sub-frame per node; XY-chart's auto-mapping then creates one series
#     per node sub-frame (own legend entry, own color from the palette).
#     This is the same transform the temperature/battery panels (16/17)
#     use to get one series per node. The earlier "No data" pitfall was
#     NOT partitionByValues' fault -- it was the XY-chart migration
#     handler corrupting the series config on panels without a
#     pluginVersion stamp; panel() now stamps pluginVersion:"13.1.0",
#     so the series config survives intact through partitionByValues.
#   * show:"points+lines": each node's dots are joined by a line. Since
#     y=node_index is constant within a series, the line is horizontal --
#     that node's "racing lane".
#   * schema declares timestamp type:"string" up front, then convertFieldType
#     re-types it to "time" -- the SAME approach Panels I (16) and F (17) use
#     (see node_split_transforms). This matches how I/F parse the naive ISO
#     timestamps: convertFieldType applies the dashboard timezone (America/
#     Toronto) when the dateFormat string has no zone designator, giving a
#     frame of epoch-ms in local-time interpretation. Declaring timestamp
#     type:"time" directly in the CSV schema instead makes the CSV plugin
#     parse naive timestamps as UTC -- a 4-hour offset from the dashboard's
#     Toronto display. That constant offset was why Panel J's packet dots
#     appeared ~4h older than the same packets in Panel I: the underlying
#     timestamps were the same; J read them as UTC, I read them as Toronto.
#
#     The earlier comment here said convertFieldType "breaks XY-chart"
#     because the converted field's type didn't propagate into the panel's
#     onlyNumTimeFields filter view (utils.ts prepSeries filters
#     field.type === number || time). That workaround was needed only
#     when pluginVersion wasn't stamped, because the XY-chart migration
#     handler then rewrote the series config; panel() now stamps
#     pluginVersion:"13.1.0", so the converted time-typed field survives
#     intact and IS picked up by onlyNumTimeFields, matching X by name.
#
#   * panel type is "timeseries" (NOT "xychart"). The earlier XY-chart
#     implementation fought Grafana's XY-chart on several fronts: it had
#     no auto-clipping to the dashboard time picker (forcing an explicit
#     filterByValue transform with the $__from/$__to variables), and -- the
#     reason it ultimately got replaced -- its time-axis `range` callback
#     (xychart/scatter.ts:323 `range: xIsTime ? (u, min, max) => [min, max]
#     : undefined`) just echoes uPlot's data-fit min/max back, which means
#     uPlot's hard/soft min/max on time axes are no-ops. So setting
#     axisSoftMin:"$__from" / axisSoftMax:"now" silently failed to extend
#     the X axis past the data extremes: opening the dashboard picker to a
#     range starting BEFORE the first packet pinned the X axis' left edge
#     to the first packet date instead of extending it further left to the
#     picker's "from" -- Panel J "stuck" at 07/15 while Panels I/F's
#     timeseries axes natively tracked the picker's "from". Switching to
#     the timeseries panel type eliminates the entire XY-chart time-axis
#     extension problem at the source: timeseries panels auto-clip and
#     auto-extend the X axis to the dashboard time picker range, exactly
#     like Panels I/F do -- no manual filter, no softMin/softMax hacks.
#
#     The "racing to now matrix" effect (one dot per packet per node,
#     horizontal lane per node) is preserved: partitionByValues(node)
#     makes each node its own timeseries, and since y=node_index is constant
#     within each series, the line is horizontal (that node's "lane") with a
#     dot at each packet event. This is the same one-series-per-node layout
#     Panels I/F use.
panels.append(panel(18, "J. Packet arrivals — racing to now (matrix)", "timeseries",
    {"h": 8, "w": 24, "x": 0, "y": 44},
    [csv_query("H", "H_packets.csv", [
        {"name": "timestamp", "type": "string"},
        {"name": "node", "type": "string"},
        {"name": "node_index", "type": "number"},
    ])],
    {
        "legend": {"displayMode": "list", "placement": "bottom",
                   "showLegend": True, "calcs": []},
        "tooltip": {"mode": "multi", "sort": "none"},
    },
    {"custom": dot_line_custom},
    node_split_transforms,
    overrides=[
        {"matcher": {"id": "byName", "options": "node_index"}, "properties": [
            {"id": "decimals", "value": 0},
            {"id": "min", "value": -0.5},
            # No hardcoded axisSoftMax: with 15 nodes (indices 0..14) a soft
            # max of 14.5 clipped the top lane off the visible axis. Soft max
            # is only a hint anyway; letting Grafana auto-fit the Y scale to
            # the actual distinct node_index values means the chart adapts to
            # whatever node count the current log has.
            {"id": "custom.axisLabel", "value": "node (index — see legend)"},
            {"id": "unit", "value": "none"},
            # Legend shows just the node address (e.g. "0x05"), not the
            # value field name plus the partition label. partitionByValues
            # with asLabels:true puts the partition value into the field's
            # labels map keyed by the partition field ("node"), so
            # ${__field.labels.node} resolves to that node's hex address.
            {"id": "displayName", "value": "${__field.labels.node}"},
        ]},
    ],
    ))

panels.append(panel(16, "I. Reported temperature over time", "timeseries",
    {"h": 8, "w": 24, "x": 0, "y": 52},
    [csv_query("I", "I_temperature.csv", [fstr("timestamp"), fstr("node"), fnum("temp_c")])],
    {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True, "calcs": []},
     "tooltip": {"mode": "multi", "sort": "none"}},
    {"custom": dot_line_custom},
    node_split_transforms,
    overrides=[
        unit_override("temp_c", "celsius"),
        {"matcher": {"id": "byName", "options": "temp_c"}, "properties": [
            {"id": "displayName", "value": "${__field.labels.node}"},
        ]},
    ]))

panels.append(panel(17, "F. Reported battery percentage over time", "timeseries",
    {"h": 8, "w": 24, "x": 0, "y": 60},
    [csv_query("F", "F_battery.csv", [fstr("timestamp"), fstr("node"), fnum("battery_pct")])],
    {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True, "calcs": []},
     "tooltip": {"mode": "multi", "sort": "none"}},
    {"custom": dot_line_custom},
    node_split_transforms,
    overrides=[
        unit_override("battery_pct", "percent", 0, 100),
        {"matcher": {"id": "byName", "options": "battery_pct"}, "properties": [
            {"id": "displayName", "value": "${__field.labels.node}"},
        ]},
    ]))

# Row 5: Gateway Info
panels.append(row(13, "Gateway Info", 68))

panels.append(panel(14, "GW. Gateway health (latest)", "gauge",
    {"h": 8, "w": 24, "x": 0, "y": 69},
    [csv_query("GW", "tioups.log", [
        fstr("Timestamp"), fnum("Battery_%"), fnum("CPU_%"), fnum("RAM_%"), fnum("Storage_%"),
    ], uid=DS_UID_LOGS)],
    {"reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""},
     "orientation": "auto", "showThresholdLabels": False, "showThresholdMarkers": True},
    {"unit": "percent", "min": 0, "max": 100,
     # CPU/RAM/Storage: high usage is bad -> red above the threshold.
     "thresholds": {"mode": "absolute", "steps": [
         {"color": "green", "value": None}, {"color": "red", "value": 80}]}},
    # Battery is the opposite of the other three: low charge is bad, so
    # it needs its own inverted thresholds (red low, green near 100%).
    overrides=[{"matcher": {"id": "byName", "options": "Battery_%"}, "properties": [
        {"id": "thresholds", "value": {"mode": "absolute", "steps": [
            {"color": "red", "value": None}, {"color": "green", "value": 20}]}},
    ]}],
    uid=DS_UID_LOGS))

dashboard = {
    "title": "ELM Network Telemetry",
    "uid": "elm-vis",
    "schemaVersion": 39,
    "version": 1,
    "timezone": "America/Toronto",
    "refresh": "5m",
    "tags": ["elm", "gateway"],
    # Default to the last 24 hours: wide enough to keep the temperature/battery
    # timeseries meaningful (they use real timestamps, so a tight range would
    # just clip them), narrow enough that the data fetch label in J_freshness
    # stays legible. The time picker in the UI can always be widened.
    "time": {"from": "now-24h", "to": "now"},
    "templating": {"list": []},
    "annotations": {"list": []},
    "panels": fold_rows(panels),
}

with open("provisioning/dashboards/elm-vis-dashboard.json", "w") as f:
    json.dump(dashboard, f, indent=2)

n_panels = sum(1 for p in panels if p["type"] != "row")
print(f"dashboard written: {len(panels)} items ({n_panels} panels)")
