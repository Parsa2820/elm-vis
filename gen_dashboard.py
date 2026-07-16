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
          transformations=None, overrides=None, uid=DS_UID):
    return {
        "id": pid, "type": ptype, "title": title, "gridPos": gridpos,
        "datasource": {"type": DS_TYPE, "uid": uid},
        "targets": targets,
        "options": options or {},
        "fieldConfig": {"defaults": (fieldconfig or {}), "overrides": overrides or []},
        "transformations": transformations or [],
    }


def row(pid, title, y):
    return {"id": pid, "type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "collapsed": False, "panels": [], "datasource": None}


def fold_rows(items):
    """Return the panel list with every row collapsed by default. Grafana
    represents a collapsed row by moving its member panels *inside* the
    row's own "panels" array (rather than leaving them as top-level
    siblings), so this regroups the flat list accordingly."""
    out = []
    current = None
    for it in items:
        if it["type"] == "row":
            it["collapsed"] = True
            it["panels"] = []
            current = it
            out.append(it)
        elif current is not None:
            current["panels"].append(it)
        else:
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

dot_line_custom = {"drawStyle": "line", "lineWidth": 1, "pointSize": 5, "showPoints": "always",
                   "fillOpacity": 10, "spanNulls": False}
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
# are each node's last-heard-from time as elapsed hours into the log --
# reusing the per-node stats elmgwplot.py already computes for packets.
panels.append(panel(18, "J. Data freshness", "table",
    {"h": 8, "w": 24, "x": 0, "y": 40},
    [csv_query("J", "J_freshness.csv", [fstr("node"), fstr("last_fetch"), fstr("last_heard_ago")])],
    {"showHeader": True, "footer": {"show": False}},
    {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}}))

panels.append(panel(16, "I. Reported temperature over time", "timeseries",
    {"h": 8, "w": 24, "x": 0, "y": 48},
    [csv_query("I", "I_temperature.csv", [fstr("timestamp"), fstr("node"), fnum("temp_c")])],
    {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True, "calcs": []},
     "tooltip": {"mode": "multi", "sort": "none"}},
    {"custom": dot_line_custom},
    node_split_transforms,
    overrides=[unit_override("temp_c", "celsius")]))

panels.append(panel(17, "F. Reported battery percentage over time", "timeseries",
    {"h": 8, "w": 24, "x": 0, "y": 56},
    [csv_query("F", "F_battery.csv", [fstr("timestamp"), fstr("node"), fnum("battery_pct")])],
    {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True, "calcs": []},
     "tooltip": {"mode": "multi", "sort": "none"}},
    {"custom": dot_line_custom},
    node_split_transforms,
    overrides=[unit_override("battery_pct", "percent", 0, 100)]))

# Row 5: Gateway Info
panels.append(row(13, "Gateway Info", 64))

panels.append(panel(14, "GW. Gateway health (latest)", "gauge",
    {"h": 8, "w": 24, "x": 0, "y": 65},
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
    "refresh": "15m",
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
