#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "matplotlib>=3.8",
#     "networkx>=3.2",
# ]
# ///
"""Plot ELM network telemetry from a gateway serial log.

Everything is derived from the gateway's own log lines (spec section 18):

    [ELM] GATEWAY data from node 0xNN (origin 0xNN, id N, signal -N dBm)
          | end-to-end delay N s | temp N C | humidity N % | battery N %
    [ELM] TOPO origin 0xNN | parents N: 0xNN 0xNN 0xNN
          (first is preferred, 0x00 unused) | rank N hops N | ch N
    [ELM] PDR origin 0xNN | rcvd N | id range a..b | est P%
    [ELM] PARENT got join request / accepting / admitting / re-admitting ...
    [ELM] freed inactive child 0xNN (silent N ms >= N ms) | DC slot N reclaimed

Produces, next to the log, a "plots/" directory holding one PNG and one (or
more) CSV per figure, so every figure can be rebuilt in pgfplots from the CSV
alone.

    A  PDR by node                 E  logical topology
    B  PDR by hop                  F  battery percent over time
    C  end-to-end delay by node    G  event timeline
       (box/bar/ecdf)             H  packet arrivals per node over time
    D  end-to-end delay by hop     I  temperature over time
       (box/bar/ecdf)

Usage:
    uv run scripts/elmgwplot.py today/tio_ttyAMA0_2026-07-08T17:59:57.log
    uv run scripts/elmgwplot.py <log> --generatedcsv generated.csv

PDR
    Default is the gateway's own `est P%` (spec 18.1): rcvd / (max_id - min_id
    + 1).  It cannot see packets lost before the first delivery or after the
    last one.  With --generatedcsv (a "node,packets" file holding each node's
    persisted EEPROM packet count) the authoritative PDR rcvd / generated is
    computed and plotted alongside it.

Duplicates
    A packet_id delivered twice is a lost-ack retransmit: it counts once in the
    PDR, once per arrival in figure H (where it is marked).

Hops
    Every Data delivery is followed by that packet's own TOPO line, so each
    delay sample and each delivered packet_id is filed under the hop count it
    actually travelled.  Lost packet_ids (gaps in the id sequence) inherit the
    hop count of the nearest delivered id.
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from math import atan, degrees
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import networkx as nx  # noqa: E402

GATEWAY_ADDR = 0x80

# --------------------------------------------------------------------------
# Regexes.  One per gateway-emitted log line we care about.
# --------------------------------------------------------------------------

RE_TS = re.compile(
    r"^\[(?P<Y>\d{4})-(?P<Mo>\d{2})-(?P<D>\d{2})T"
    r"(?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2})\.(?P<MS>\d{3})\]\s?(?P<body>.*)$"
)

RE_DATA = re.compile(
    r"\[ELM\] GATEWAY data from node 0x(?P<sender>[0-9A-Fa-f]{2}) "
    r"\(origin 0x(?P<origin>[0-9A-Fa-f]{2}), id (?P<pid>\d+), "
    r"signal (?P<rssi>-?\d+) dBm\) \| end-to-end delay (?P<delay>\d+) s \| "
    r"temp (?P<temp>-?\d+\.\d+) C \| humidity (?P<hum>-?\d+\.\d+) % \| "
    r"battery (?P<batt>\d+) %"
)

RE_TOPO = re.compile(
    r"\[ELM\] TOPO origin 0x(?P<origin>[0-9A-Fa-f]{2}) \| "
    r"parents (?P<pcount>\d+): (?P<plist>(?:0x[0-9A-Fa-f]{2} ?)+)"
    r"\(first is preferred, 0x00 unused\) \| "
    r"rank (?P<rank>\d+) hops (?P<hops>\d+) \| ch (?P<ch>\d+)"
)

RE_PDR = re.compile(
    r"\[ELM\] PDR origin 0x(?P<origin>[0-9A-Fa-f]{2}) \| rcvd (?P<rcvd>\d+) \| "
    r"id range (?P<lo>\d+)\.\.(?P<hi>\d+) \| est (?P<est>\d+)%"
)

RE_CLOCK = re.compile(
    r"\[ELM\] GATEWAY clock \d+:\d+:\d+\.\d+ \(up (?P<h>\d+)h(?P<m>\d+)m(?P<s>\d+)s\) \| "
    r"data rcvd (?P<data>\d+) \(acked (?P<dacked>\d+)\) \| "
    r"keepalives rcvd (?P<ka>\d+) \(acked (?P<kaacked>\d+)\)"
)

# Event lines.  Each maps to (event name, node-address group, detail builder).
RE_JOIN_REQ = re.compile(
    r"\[ELM\] PARENT got join request from node 0x(?P<node>[0-9A-Fa-f]{2}) "
    r"\(signal (?P<rssi>-?\d+) dBm\)"
)
RE_JOIN_ACCEPT = re.compile(
    r"\[ELM\] PARENT accepting node 0x(?P<node>[0-9A-Fa-f]{2}), sending join reply"
)
RE_JOIN_ADMIT = re.compile(
    r"\[ELM\] PARENT admitting child 0x(?P<node>[0-9A-Fa-f]{2}) "
    r"\(signal (?P<rssi>-?\d+) dBm\), DC slot (?P<slot>\d+) "
    r"\((?P<n>\d+) of (?P<max>\d+) children\)"
)
RE_JOIN_READMIT = re.compile(
    r"\[ELM\] PARENT re-admitting child 0x(?P<node>[0-9A-Fa-f]{2}) \(DC slot (?P<slot>\d+)\)"
)
RE_REJECT_PARENT = re.compile(
    r"\[ELM\] PARENT rejecting join from 0x(?P<node>[0-9A-Fa-f]{2}): it is our parent"
)
RE_REJECT_CAP = re.compile(
    r"\[ELM\] PARENT at child capacity \((?P<cap>\d+)\), "
    r"rejecting join from 0x(?P<node>[0-9A-Fa-f]{2})"
)
RE_REJECT_SLOT = re.compile(
    r"\[ELM\] PARENT no free slot for a DC, rejecting join from 0x(?P<node>[0-9A-Fa-f]{2})"
)
RE_CHILD_FREED = re.compile(
    r"\[ELM\] freed inactive child 0x(?P<node>[0-9A-Fa-f]{2}) "
    r"\(silent (?P<silent>\d+) ms >= (?P<timeout>\d+) ms\) \| "
    r"DC slot (?P<slot>\d+) reclaimed"
)
RE_BUF_FULL = re.compile(
    r"\[ELM\] data buffer full \((?P<n>\d+)\), "
    r"not acking relayed data from 0x(?P<node>[0-9A-Fa-f]{2})"
)
RE_SR_SENT = re.compile(
    r"\[ELM\] NODE sent slot reassignment to child 0x(?P<node>[0-9A-Fa-f]{2}) "
    r"\(-> DC slot (?P<slot>\d+)\)"
)
RE_SR_ACKED = re.compile(
    r"\[ELM\] NODE slot reassignment acked by child 0x(?P<node>[0-9A-Fa-f]{2}) \| "
    r"child on DC slot (?P<slot>\d+)"
)
RE_DFF_SENT = re.compile(
    r"\[ELM\] NODE sent data forward failure to child 0x(?P<node>[0-9A-Fa-f]{2}) "
    r"\(origin 0x(?P<origin>[0-9A-Fa-f]{2}), id (?P<pid>\d+)\)"
)
RE_TX_FAIL = re.compile(r"\[ELM\] WARNING: radio send failed or timed out")
RE_BOOT = re.compile(r"\[ELM\] Starting up as (?P<role>\w+)")

# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass
class Delivery:
    """One `GATEWAY data` line, joined with the TOPO line that follows it."""

    t: float  # elapsed seconds since first log line
    ts: str  # real wall-clock time of this line, "YYYY-MM-DD HH:MM:SS.mmm"
    sender: int  # the child that handed the packet to the gateway
    origin: int
    pid: int
    rssi: int
    delay_s: int
    temp_c: float
    humidity: float
    battery: int
    rank: int | None = None
    hops: int | None = None
    parents: tuple[int, ...] = ()
    channel: int | None = None


@dataclass
class Event:
    t: float
    clock: str
    node: int
    kind: str
    detail: str


@dataclass
class OriginPdr:
    """Last `PDR origin` line seen for one origin."""

    rcvd: int = 0
    lo: int = 0
    hi: int = 0
    est: int = 0


@dataclass
class TopoState:
    parents: tuple[int, ...] = ()
    rank: int = 0
    hops: int = 0
    channel: int = 0
    t: float = 0.0


@dataclass
class Parsed:
    deliveries: list[Delivery] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    pdr: dict[int, OriginPdr] = field(default_factory=dict)
    topo: dict[int, TopoState] = field(default_factory=dict)
    uptime_s: float = 0.0
    lines: int = 0
    unmatched_topo: int = 0
    # ISO 8601 wall-clock timestamp of the last timestamped line in the log,
    # NOT just the last GATEWAY data delivery -- TOPO/PDR/event/trailing
    # lines count too. Consumed by fetch-and-plot.sh for the dashboard's
    # "last line of log time" indicator. Unlike deliveries[-1].ts this is
    # the true end-of-log time and stays current even if the log stops with
    # non-delivery lines.
    last_line_ts: str = ""
    # Elapsed seconds (since the first timestamped line, t0) of the last
    # timestamped line -- the analog of Delivery.t but for ANY line, not just
    # deliveries. fig_h_packets uses this as the true "log end" so per-node
    # last_heard_ago_h measures the gap to the END OF THE LOG, not (as
    # before) to the last delivery. With logs that keep emitting PDR/event
    # lines after the last GATEWAY-data delivery, the old delivery-anchored
    # computation understated staleness by however long that tail is (often
    # hours); the freshness table now reports the real age.
    last_t: float = 0.0


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_parents(plist: str) -> tuple[int, ...]:
    """`0x80 0x02 0x00` -> (0x80, 0x02); 0x00 is the unused-slot padding."""
    addrs = [int(a, 16) for a in re.findall(r"0x([0-9A-Fa-f]{2})", plist)]
    return tuple(a for a in addrs if a != 0x00)


def parse_log(path: Path) -> Parsed:
    out = Parsed()
    t0 = None
    pending: Delivery | None = None  # last Data line, awaiting its TOPO line

    def emit(t: float, clock: str, node: int, kind: str, detail: str = "") -> None:
        out.events.append(Event(t, clock, node, kind, detail))

    with path.open("r", errors="replace") as fh:
        for raw in fh:
            m = RE_TS.match(raw.rstrip("\r\n"))
            if not m:
                continue
            out.lines += 1
            hh, mm, ss, ms = (int(m.group(i)) for i in ("H", "M", "S", "MS"))
            secs = hh * 3600 + mm * 60 + ss + ms / 1000.0
            abs_s = int(m.group("Y")) * 31622400 + int(m.group("Mo")) * 2629800 + int(
                m.group("D")
            ) * 86400 + secs
            if t0 is None:
                t0 = abs_s
            t = abs_s - t0
            clock = f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"
            body = m.group("body")
            real_ts = datetime(
                int(m.group("Y")), int(m.group("Mo")), int(m.group("D")), hh, mm, ss, ms * 1000
            )
            ts = real_ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]  # ISO 8601, Grafana parses this reliably
            # Every timestamped line updates last_line_ts / last_t, so they
            # track the true end of the log regardless of line kind
            # (Data/TOPO/PDR/event). last_line_ts is the value the dashboard
            # surfaces as "last line of log time"; last_t lets fig_h_packets
            # anchor last_heard_ago_h to the real log end, not to the last
            # delivery (deliveries[-1].ts would miss trailing non-delivery
            # lines and understate each node's age).
            out.last_line_ts = ts
            out.last_t = t

            if (g := RE_DATA.search(body)) is not None:
                pending = Delivery(
                    t=t,
                    ts=ts,
                    sender=int(g["sender"], 16),
                    origin=int(g["origin"], 16),
                    pid=int(g["pid"]),
                    rssi=int(g["rssi"]),
                    delay_s=int(g["delay"]),
                    temp_c=float(g["temp"]),
                    humidity=float(g["hum"]),
                    battery=int(g["batt"]),
                )
                out.deliveries.append(pending)
                continue

            if (g := RE_TOPO.search(body)) is not None:
                origin = int(g["origin"], 16)
                parents = parse_parents(g["plist"])
                rank, hops, ch = int(g["rank"]), int(g["hops"]), int(g["ch"])
                if pending is not None and pending.origin == origin:
                    pending.rank, pending.hops = rank, hops
                    pending.parents, pending.channel = parents, ch
                    pending = None
                else:
                    out.unmatched_topo += 1
                emit_topo_deltas(out, t, clock, origin, parents, rank, hops, ch, emit)
                out.topo[origin] = TopoState(parents, rank, hops, ch, t)
                continue

            pending = None  # any other line breaks the Data/TOPO adjacency

            if (g := RE_PDR.search(body)) is not None:
                out.pdr[int(g["origin"], 16)] = OriginPdr(
                    rcvd=int(g["rcvd"]),
                    lo=int(g["lo"]),
                    hi=int(g["hi"]),
                    est=int(g["est"]),
                )
                continue

            if (g := RE_CLOCK.search(body)) is not None:
                out.uptime_s = int(g["h"]) * 3600 + int(g["m"]) * 60 + int(g["s"])
                continue

            if (g := RE_JOIN_REQ.search(body)) is not None:
                emit(t, clock, int(g["node"], 16), "join request", f"{g['rssi']} dBm")
            elif (g := RE_JOIN_ACCEPT.search(body)) is not None:
                emit(t, clock, int(g["node"], 16), "join accepted", "")
            elif (g := RE_JOIN_ADMIT.search(body)) is not None:
                emit(
                    t,
                    clock,
                    int(g["node"], 16),
                    "child admitted",
                    f"DC slot {g['slot']}, {g['n']}/{g['max']} children, {g['rssi']} dBm",
                )
            elif (g := RE_JOIN_READMIT.search(body)) is not None:
                emit(t, clock, int(g["node"], 16), "child re-admitted", f"DC slot {g['slot']}")
            elif (g := RE_REJECT_PARENT.search(body)) is not None:
                emit(t, clock, int(g["node"], 16), "join rejected", "it is our parent")
            elif (g := RE_REJECT_CAP.search(body)) is not None:
                emit(t, clock, int(g["node"], 16), "join rejected", f"at capacity {g['cap']}")
            elif (g := RE_REJECT_SLOT.search(body)) is not None:
                emit(t, clock, int(g["node"], 16), "join rejected", "no free DC slot")
            elif (g := RE_CHILD_FREED.search(body)) is not None:
                emit(
                    t,
                    clock,
                    int(g["node"], 16),
                    "child left",
                    f"silent {int(g['silent']) / 1000:.0f} s, DC slot {g['slot']} reclaimed",
                )
            elif (g := RE_BUF_FULL.search(body)) is not None:
                emit(t, clock, int(g["node"], 16), "buffer full", f"{g['n']} entries")
            elif (g := RE_SR_SENT.search(body)) is not None:
                emit(t, clock, int(g["node"], 16), "slot reassignment", f"-> DC slot {g['slot']}")
            elif (g := RE_SR_ACKED.search(body)) is not None:
                emit(t, clock, int(g["node"], 16), "slot reassignment", f"acked, DC slot {g['slot']}")
            elif (g := RE_DFF_SENT.search(body)) is not None:
                emit(
                    t,
                    clock,
                    int(g["node"], 16),
                    "forward failure",
                    f"origin 0x{int(g['origin'], 16):02X} id {g['pid']}",
                )
            elif RE_TX_FAIL.search(body) is not None:
                emit(t, clock, GATEWAY_ADDR, "tx failure", "radio send failed or timed out")
            elif (g := RE_BOOT.search(body)) is not None:
                emit(t, clock, GATEWAY_ADDR, "boot", f"role {g['role']}")

    if t0 is None:
        sys.exit(f"{path}: no timestamped lines found")
    return out


def emit_topo_deltas(out, t, clock, origin, parents, rank, hops, ch, emit) -> None:
    """Derive per-origin events by diffing consecutive TOPO reports."""
    prev = out.topo.get(origin)
    if prev is None:
        pretty = " ".join(f"0x{p:02X}" for p in parents)
        emit(t, clock, origin, "first report", f"rank {rank}, {hops} hops, parents {pretty}")
        return
    if prev.rank != rank:
        emit(t, clock, origin, "rank change", f"{prev.rank} -> {rank}")
    if prev.hops != hops:
        emit(t, clock, origin, "hops change", f"{prev.hops} -> {hops}")
    if prev.parents[:1] != parents[:1]:
        old = f"0x{prev.parents[0]:02X}" if prev.parents else "none"
        new = f"0x{parents[0]:02X}" if parents else "none"
        emit(t, clock, origin, "parent change", f"preferred {old} -> {new}")
    if prev.parents[1:] != parents[1:]:
        old = " ".join(f"0x{p:02X}" for p in prev.parents[1:]) or "none"
        new = " ".join(f"0x{p:02X}" for p in parents[1:]) or "none"
        emit(t, clock, origin, "backup change", f"backups {old} -> {new}")
    if prev.channel != ch:
        emit(t, clock, origin, "channel change", f"ch {prev.channel} -> {ch}")


def read_generated(path: Path) -> dict[int, int]:
    """`node,packets` CSV of each node's persisted EEPROM packet count."""
    out: dict[int, int] = {}
    with path.open(newline="") as fh:
        for row in csv.reader(fh):
            if not row or row[0].lstrip().startswith("#"):
                continue
            node, packets = row[0].strip(), row[1].strip()
            if node.lower() in ("node", "origin", "addr", "address"):
                continue  # header
            out[int(node, 16) if node.lower().startswith("0x") else int(node)] = int(packets)
    return out


# --------------------------------------------------------------------------
# Derived metrics
# --------------------------------------------------------------------------

Stats = dict[str, float]


def hop_of_id(ids: list[int], hops: list[int], target: int) -> int:
    """Hop count of the delivered id nearest to `target` (ties -> lower id)."""
    i = bisect_left(ids, target)
    if i == 0:
        return hops[0]
    if i == len(ids):
        return hops[-1]
    lo, hi = ids[i - 1], ids[i]
    return hops[i - 1] if (target - lo) <= (hi - target) else hops[i]


def per_origin_ids(deliveries: list[Delivery]) -> dict[int, dict[int, int | None]]:
    """origin -> {packet_id: hops of its first delivery}."""
    out: dict[int, dict[int, int | None]] = defaultdict(dict)
    for d in deliveries:
        out[d.origin].setdefault(d.pid, d.hops)
    return out


def pdr_by_node(p: Parsed, generated: dict[int, int]) -> list[dict]:
    ids = per_origin_ids(p.deliveries)
    rows = []
    for origin in sorted(set(p.pdr) | set(ids)):
        seen = ids.get(origin, {})
        lines = sum(1 for d in p.deliveries if d.origin == origin)
        st = p.pdr.get(origin)
        rcvd = st.rcvd if st else len(seen)
        lo = st.lo if st else (min(seen) if seen else 0)
        hi = st.hi if st else (max(seen) if seen else 0)
        est = st.est if st else 0
        row = {
            "node": f"0x{origin:02X}",
            "addr": origin,
            "rcvd": rcvd,
            "delivery_lines": lines,
            "duplicates": lines - len(seen),
            "id_min": lo,
            "id_max": hi,
            "id_span": hi - lo + 1,
            "est_pdr_pct": est,
            "generated": "",
            "authoritative_pdr_pct": "",
            "counter_reset": "",
        }
        if origin in generated:
            gen = generated[origin]
            row["generated"] = gen
            row["authoritative_pdr_pct"] = round(100.0 * rcvd / gen, 2) if gen else ""
            row["counter_reset"] = "yes" if lo == 0 else "no"
        rows.append(row)
    return rows


def pdr_by_hop(p: Parsed, generated: dict[int, int]) -> tuple[list[dict], list[str]]:
    """Delivered/expected per hop count, gaps attributed to the nearest delivery."""
    warnings: list[str] = []
    delivered: dict[int, int] = defaultdict(int)
    lost: dict[int, int] = defaultdict(int)
    nodes: dict[int, set[int]] = defaultdict(set)

    for origin, seen in sorted(per_origin_ids(p.deliveries).items()):
        known = sorted(pid for pid, h in seen.items() if h is not None)
        if not known:
            warnings.append(f"0x{origin:02X}: no TOPO-matched delivery, excluded from by-hop PDR")
            continue
        hops = [seen[pid] for pid in known]
        lo, hi = min(seen), max(seen)
        if origin in generated:
            gen = generated[origin]
            if lo != 0:
                warnings.append(
                    f"0x{origin:02X}: first delivered id is {lo}, not 0 — its EEPROM counter was "
                    f"not reset, so `generated={gen}` also counts packets originated before this "
                    "log; its authoritative PDR is a lower bound"
                )
            if gen < hi + 1:
                warnings.append(
                    f"0x{origin:02X}: generated={gen} < max id {hi} (stale batched flush, "
                    "spec 18.1) — using the observed id window instead"
                )
                universe = range(lo, hi + 1)
            else:
                universe = range(0, gen)
        else:
            universe = range(lo, hi + 1)

        for pid in universe:
            if pid in seen:
                h = seen[pid]
                if h is None:
                    continue  # delivered but never TOPO-matched: neither hit nor miss
                delivered[h] += 1
            else:
                h = hop_of_id(known, hops, pid)
                lost[h] += 1
            nodes[h].add(origin)

    rows = []
    for h in sorted(set(delivered) | set(lost)):
        exp = delivered[h] + lost[h]
        rows.append(
            {
                "hops": h,
                "delivered": delivered[h],
                "lost": lost[h],
                "expected": exp,
                "pdr_pct": round(100.0 * delivered[h] / exp, 2) if exp else 0.0,
                "nodes": " ".join(f"0x{n:02X}" for n in sorted(nodes[h])),
            }
        )
    return rows, warnings


def describe(samples: list[float]) -> Stats:
    s = sorted(samples)
    n = len(s)
    if n == 1:
        q1 = med = q3 = s[0]
    else:
        q1, med, q3 = statistics.quantiles(s, n=4, method="inclusive")
    iqr = q3 - q1
    lo_fence, hi_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inliers = [v for v in s if lo_fence <= v <= hi_fence] or s
    return {
        "n": n,
        "mean": statistics.fmean(s),
        "std": statistics.stdev(s) if n > 1 else 0.0,
        "min": s[0],
        "q1": q1,
        "median": med,
        "q3": q3,
        "max": s[-1],
        "p95": s[min(n - 1, int(round(0.95 * (n - 1))))],
        "lower_whisker": min(inliers),
        "upper_whisker": max(inliers),
        "outliers": sorted({v for v in s if v < lo_fence or v > hi_fence}),
    }


def ecdf(samples: list[float]) -> list[tuple[float, float]]:
    s = sorted(samples)
    n = len(s)
    out, i = [], 0
    while i < n:
        v = s[i]
        while i < n and s[i] == v:
            i += 1
        out.append((v, i / n))
    return out


# --------------------------------------------------------------------------
# CSV / plot helpers
# --------------------------------------------------------------------------


def write_csv(path: Path, header: list[str], rows) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


class Out:
    def __init__(self, outdir: Path, fmt: str, dpi: int) -> None:
        self.dir, self.fmt, self.dpi = outdir, fmt, dpi
        self.written: list[Path] = []

    def csv(self, name: str, header: list[str], rows) -> None:
        p = self.dir / f"{name}.csv"
        write_csv(p, header, rows)
        self.written.append(p)

    def fig(self, name: str, fig, dpi: int | None = None) -> None:
        p = self.dir / f"{name}.{self.fmt}"
        fig.savefig(p, dpi=dpi or self.dpi, bbox_inches="tight")
        plt.close(fig)
        self.written.append(p)


def hexlabel(a: int) -> str:
    return f"0x{a:02X}"


def bar_labels(ax, bars, fmt="{:.0f}", tops=None, boxed=False) -> None:
    """Annotate each bar with its height, optionally above `tops` (e.g. an
    error-bar cap) and on an opaque patch so the whisker does not pierce it."""
    bbox = dict(facecolor="white", edgecolor="none", alpha=0.7, pad=1.0) if boxed else None
    for i, b in enumerate(bars):
        y = b.get_height() if tops is None else tops[i]
        ax.annotate(
            fmt.format(b.get_height()),
            (b.get_x() + b.get_width() / 2, y),
            ha="center",
            va="bottom",
            fontsize=8,
            bbox=bbox,
            zorder=5,
        )


# --------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------


def fig_a_pdr_by_node(out: Out, rows: list[dict]) -> None:
    out.csv(
        "A_pdr_by_node",
        list(rows[0].keys()) if rows else ["node"],
        [list(r.values()) for r in rows],
    )
    labels = [r["node"] for r in rows]
    est = [r["est_pdr_pct"] for r in rows]
    auth = [r["authoritative_pdr_pct"] for r in rows]
    has_auth = any(a != "" for a in auth)

    fig, ax = plt.subplots(figsize=(7, 3.6))
    x = range(len(labels))
    if has_auth:
        w = 0.38
        b1 = ax.bar([i - w / 2 for i in x], est, w, label="estimated (id window)")
        b2 = ax.bar(
            [i + w / 2 for i in x],
            [a if a != "" else 0 for a in auth],
            w,
            label="authoritative (EEPROM count)",
        )
        bar_labels(ax, b1)
        bar_labels(ax, b2)
        ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1.0))
    else:
        bar_labels(ax, ax.bar(list(x), est, 0.6))
    ax.set_xticks(list(x), labels)
    ax.set_xlabel("node")
    ax.set_ylabel("PDR (%)")
    ax.set_ylim(0, 108)
    ax.set_title("Packet delivery ratio by node")
    out.fig("A_pdr_by_node", fig)


def fig_b_pdr_by_hop(out: Out, rows: list[dict]) -> None:
    out.csv(
        "B_pdr_by_hop",
        list(rows[0].keys()) if rows else ["hops"],
        [list(r.values()) for r in rows],
    )
    labels = [str(r["hops"]) for r in rows]
    fig, ax = plt.subplots(figsize=(5, 3.6))
    bars = ax.bar(labels, [r["pdr_pct"] for r in rows], 0.55)
    bar_labels(ax, bars)
    pad = 0.05 * max((b.get_height() for b in bars), default=100.0)
    for b, r in zip(bars, rows):
        ax.annotate(
            f"{r['delivered']}/{r['expected']}",
            (b.get_x() + b.get_width() / 2, pad),
            ha="center",
            va="bottom",
            fontsize=7,
            color="white",
        )
    ax.set_xlabel("hops to gateway")
    ax.set_ylabel("PDR (%)")
    ax.set_ylim(0, 108)
    ax.set_title("Packet delivery ratio by hop count")
    out.fig("B_pdr_by_hop", fig)


def delay_figures(out: Out, prefix: str, title: str, xlabel: str, groups: dict[str, list[int]]) -> None:
    """Box, mean+/-std bar, and ECDF for one grouping of the delay samples."""
    keys = list(groups)
    stats = {k: describe([float(v) for v in groups[k]]) for k in keys}
    # pgfplots can only filter rows on a numeric column, hence group_index.
    gi = {k: i for i, k in enumerate(keys)}

    out.csv(
        f"{prefix}_stats",
        [
            xlabel, "group_index", "n", "mean", "std", "std_lower", "min",
            "lower_whisker", "q1", "median", "q3", "upper_whisker", "p95",
            "max", "outliers",
        ],
        [
            [
                k, gi[k], s["n"], round(s["mean"], 3), round(s["std"], 3),
                # Delay >= 0, so the drawn lower error arm is clipped at the axis.
                round(min(s["mean"], s["std"]), 3),
                s["min"], s["lower_whisker"], s["q1"], s["median"], s["q3"],
                s["upper_whisker"], s["p95"], s["max"],
                ";".join(f"{v:g}" for v in s["outliers"]),
            ]
            for k, s in stats.items()
        ],
    )
    # One row per outlier: pgfplots `boxplot prepared` wants them as a table.
    out.csv(
        f"{prefix}_outliers",
        [xlabel, "group_index", "delay_s"],
        [[k, gi[k], f"{v:g}"] for k, s in stats.items() for v in s["outliers"]],
    )
    out.csv(
        f"{prefix}_ecdf",
        [xlabel, "group_index", "delay_s", "cdf"],
        [[k, gi[k], v, round(c, 6)] for k in keys for v, c in ecdf(groups[k])],
    )

    fig, ax = plt.subplots(figsize=(max(5, 0.9 * len(keys) + 2), 3.6))
    ax.boxplot([groups[k] for k in keys], tick_labels=keys, showfliers=True)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("end-to-end delay (s)")
    ax.set_title(f"{title} (box)")
    out.fig(f"{prefix}_box", fig)

    fig, ax = plt.subplots(figsize=(max(5, 0.9 * len(keys) + 2), 3.6))
    means = [stats[k]["mean"] for k in keys]
    stds = [stats[k]["std"] for k in keys]
    # Delay cannot be negative: clip the lower whisker at 0 (asymmetric yerr).
    yerr = [[min(m, s) for m, s in zip(means, stds)], stds]
    bars = ax.bar(keys, means, 0.55, yerr=yerr, capsize=4, error_kw=dict(zorder=4))
    bar_labels(ax, bars, "{:.1f}", tops=[m + s for m, s in zip(means, stds)], boxed=True)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("end-to-end delay (s)")
    ax.margins(y=0.14)  # headroom for the boxed label; must precede set_ylim
    ax.set_ylim(bottom=0)
    ax.set_title(f"{title} (mean $\\pm$ std)")
    out.fig(f"{prefix}_bar", fig)

    fig, ax = plt.subplots(figsize=(5.5, 3.6))
    for k in keys:
        xs, ys = zip(*ecdf(groups[k]))
        ax.step([0, *xs], [0, *ys], where="post", label=k)
    ax.set_xlabel("end-to-end delay (s)")
    ax.set_ylabel("cumulative fraction")
    ax.set_ylim(0, 1.02)
    ax.legend(title=xlabel, frameon=False, fontsize=8, ncol=2)
    ax.set_title(f"{title} (ECDF)")
    out.fig(f"{prefix}_ecdf", fig)


def fig_cd_delay(out: Out, p: Parsed) -> None:
    out.csv(
        "C_delay_samples",
        ["elapsed_h", "origin", "sender", "packet_id", "hops", "rank", "delay_s"],
        [
            [
                round(d.t / 3600, 6), hexlabel(d.origin), hexlabel(d.sender),
                d.pid, "" if d.hops is None else d.hops,
                "" if d.rank is None else d.rank, d.delay_s,
            ]
            for d in p.deliveries
        ],
    )

    by_node: dict[str, list[int]] = defaultdict(list)
    for d in p.deliveries:
        by_node[hexlabel(d.origin)].append(d.delay_s)
    by_node = {k: by_node[k] for k in sorted(by_node)}
    delay_figures(out, "C_delay_by_node", "End-to-end delay by node", "node", by_node)

    by_hop: dict[str, list[int]] = defaultdict(list)
    for d in p.deliveries:
        if d.hops is not None:
            by_hop[str(d.hops)].append(d.delay_s)
    by_hop = {k: by_hop[k] for k in sorted(by_hop, key=int)}
    if by_hop:
        delay_figures(out, "D_delay_by_hop", "End-to-end delay by hop count", "hops", by_hop)


def fig_e_topology(out: Out, p: Parsed) -> None:
    """Union of each origin's latest TOPO report: preferred tree + backup links."""
    hops = {GATEWAY_ADDR: 0}
    for origin, st in p.topo.items():
        hops[origin] = st.hops
    for origin, st in p.topo.items():  # a pure forwarder never originates
        for parent in st.parents:
            hops.setdefault(parent, max(0, st.hops - 1))

    edges = []
    for origin, st in sorted(p.topo.items()):
        for i, parent in enumerate(st.parents):
            edges.append((origin, parent, "preferred" if i == 0 else "backup", st))

    g = nx.DiGraph()
    for n in sorted(hops):
        g.add_node(n, layer=hops[n])
    for c, par, kind, _ in edges:
        g.add_edge(c, par, kind=kind)

    pos = nx.multipartite_layout(g, subset_key="layer", align="horizontal")
    pos = {n: (x, -y) for n, (x, y) in pos.items()}  # gateway on top

    # Backup edges converging on one parent overlap if they share a curvature,
    # so fan them out: alternate sign, grow the radius with each pair.
    incoming: dict[int, list[int]] = defaultdict(list)
    for u, v, kind, _ in edges:
        if kind == "backup":
            incoming[v].append(u)
    rads: dict[tuple[int, int], float] = {}
    for v, sources in incoming.items():
        for i, u in enumerate(sorted(sources, key=lambda n: pos[n][0])):
            rads[(u, v)] = (0.14 + 0.13 * (i // 2)) * (1 if i % 2 == 0 else -1)

    out.csv(
        "E_topology_edges",
        # "target"/"source" (child/parent) are named for Grafana's geomap
        # Network layer, which detects the edges frame by a field literally
        # named "source" and links edges via "source"/"target" -- see
        # gen_dashboard.py's topology panel.
        [
            "target", "source", "kind", "child_rank", "child_hops",
            "child_channel", "report_h", "bend_deg",
        ],
        [
            [
                hexlabel(c), hexlabel(par), kind, st.rank, st.hops, st.channel,
                round(st.t / 3600, 4),
                # matplotlib `arc3,rad=r` <-> tikz `bend left=atan(2r)`.
                round(degrees(atan(2 * rads.get((c, par), 0.0)))),
            ]
            for c, par, kind, st in edges
        ],
    )

    # The leftmost node of each layer carries that layer's caption, so a
    # pgfplots rebuild can place the "1 hop" labels without a second pass.
    leftmost = {}
    for n in sorted(hops):
        h = hops[n]
        if h not in leftmost or pos[n][0] < pos[leftmost[h]][0]:
            leftmost[h] = n

    def layer_label(n: int) -> str:
        h = hops[n]
        if leftmost[h] != n:
            return ""
        return "gateway" if h == 0 else f"{h} hop{'s' if h > 1 else ''}"

    out.csv(
        "E_topology_nodes",
        ["node", "role", "rank", "hops", "channel", "parents", "x", "y", "layer_label"],
        [
            [
                hexlabel(n),
                "gateway" if n == GATEWAY_ADDR else "node",
                p.topo[n].rank if n in p.topo else 0,
                hops[n],
                p.topo[n].channel if n in p.topo else "",
                " ".join(hexlabel(x) for x in p.topo[n].parents) if n in p.topo else "",
                round(pos[n][0], 4),
                round(pos[n][1], 4),
                layer_label(n),
            ]
            for n in sorted(hops)
        ],
    )

    # Arrowheads must terminate on the node's rim, so every edge draw is told
    # the real node sizes; networkx otherwise assumes 300 and buries the head
    # inside the marker.
    gw = [n for n in g if n == GATEWAY_ADDR]
    others = [n for n in g if n != GATEWAY_ADDR]
    nodelist = list(g.nodes())
    node_size = {n: (1500 if n == GATEWAY_ADDR else 1100) for n in nodelist}
    sizes = [node_size[n] for n in nodelist]

    fig, ax = plt.subplots(figsize=(8.5, 5.4))
    pref = [(u, v) for u, v, d in g.edges(data=True) if d["kind"] == "preferred"]
    back = [(u, v) for u, v, d in g.edges(data=True) if d["kind"] == "backup"]
    edge_kw = dict(
        ax=ax,
        arrows=True,
        arrowstyle="-|>",
        nodelist=nodelist,
        node_size=sizes,
        min_source_margin=2,
        min_target_margin=2,
    )
    nx.draw_networkx_edges(
        g, pos, edgelist=pref, arrowsize=16, width=1.6, edge_color="#222222", **edge_kw
    )
    for u, v in back:
        nx.draw_networkx_edges(
            g, pos, edgelist=[(u, v)], arrowsize=12, width=1.0,
            style="dashed", edge_color="#666666",
            connectionstyle=f"arc3,rad={rads[(u, v)]}", **edge_kw,
        )
    nx.draw_networkx_nodes(
        g, pos, nodelist=gw, ax=ax, node_shape="s", node_size=node_size[GATEWAY_ADDR],
        node_color="#d9d9d9", edgecolors="black", linewidths=1.2,
    )
    nx.draw_networkx_nodes(
        g, pos, nodelist=others, ax=ax, node_size=[node_size[n] for n in others],
        node_color="white", edgecolors="black", linewidths=1.2,
    )
    nx.draw_networkx_labels(g, pos, {n: hexlabel(n) for n in g}, ax=ax, font_size=9)

    for h in sorted(set(hops.values())):
        ys = [pos[n][1] for n in g if hops[n] == h]
        ax.annotate(
            "gateway" if h == 0 else f"{h} hop{'s' if h > 1 else ''}",
            (min(x for x, _ in pos.values()) - 0.14, ys[0]),
            ha="right", va="center", fontsize=9, color="gray",
        )
    ax.plot([], [], "-", color="#222222", linewidth=1.6, label="preferred parent")
    ax.plot([], [], "--", color="#666666", linewidth=1.0, label="backup parent")
    ax.legend(frameon=False, fontsize=9, loc="lower right", title="child $\\rightarrow$ parent")
    ax.get_legend().get_title().set_fontsize(9)
    ax.set_title("Logical topology (latest report per node)")
    ax.set_axis_off()
    ax.margins(0.10)
    out.fig("E_topology", fig, dpi=max(out.dpi, 220))


def fig_f_battery(out: Out, p: Parsed) -> None:
    series: dict[int, list[tuple[float, str, int]]] = defaultdict(list)
    for d in p.deliveries:
        series[d.origin].append((d.t / 3600, d.ts, d.battery))
    index = {o: i for i, o in enumerate(sorted(series))}

    out.csv(
        "F_battery",
        ["elapsed_h", "timestamp", "node", "node_index", "battery_pct"],
        [
            [round(t, 6), ts, hexlabel(o), index[o], b]
            for o in sorted(series)
            for t, ts, b in series[o]
        ],
    )
    out.csv("F_battery_nodes", ["node_index", "node"], [[i, hexlabel(o)] for o, i in index.items()])

    fig, ax = plt.subplots(figsize=(7, 3.8))
    for o in sorted(series):
        xs, _, ys = zip(*series[o])
        ax.plot(xs, ys, marker=".", markersize=2, linewidth=1, label=hexlabel(o))
    ax.set_xlabel("elapsed time (h)")
    ax.set_ylabel("battery (%)")
    ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1.0))
    ax.set_title("Battery state of charge over time")
    out.fig("F_battery", fig)


def fig_i_temperature(out: Out, p: Parsed) -> None:
    series: dict[int, list[tuple[float, str, float]]] = defaultdict(list)
    for d in p.deliveries:
        series[d.origin].append((d.t / 3600, d.ts, d.temp_c))
    index = {o: i for i, o in enumerate(sorted(series))}

    out.csv(
        "I_temperature",
        ["elapsed_h", "timestamp", "node", "node_index", "temp_c"],
        [
            [round(t, 6), ts, hexlabel(o), index[o], c]
            for o in sorted(series)
            for t, ts, c in series[o]
        ],
    )
    out.csv("I_temperature_nodes", ["node_index", "node"], [[i, hexlabel(o)] for o, i in index.items()])

    fig, ax = plt.subplots(figsize=(7, 3.8))
    for o in sorted(series):
        xs, _, ys = zip(*series[o])
        ax.plot(xs, ys, marker=".", markersize=2, linewidth=1, label=hexlabel(o))
    ax.set_xlabel("elapsed time (h)")
    ax.set_ylabel("temperature (C)")
    ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1.0))
    ax.set_title("Reported temperature over time")
    out.fig("I_temperature", fig)


EVENT_STYLE = {
    "boot": ("*", 90),
    "first report": ("o", 30),
    "join request": ("^", 34),
    "join accepted": ("v", 34),
    "child admitted": ("s", 34),
    "child re-admitted": ("s", 34),
    "join rejected": ("x", 40),
    "child left": ("X", 48),
    "rank change": ("d", 34),
    "hops change": ("d", 34),
    "parent change": ("P", 44),
    "backup change": ("p", 38),
    "channel change": ("h", 34),
    "buffer full": ("1", 50),
    "slot reassignment": ("2", 50),
    "forward failure": ("3", 50),
    "tx failure": ("4", 50),
}


def fig_g_events(out: Out, p: Parsed) -> None:
    if not p.events:
        out.csv("G_events", ["elapsed_h", "timestamp", "node", "node_index", "event", "event_id", "detail"], [])
        return

    nodes = sorted({e.node for e in p.events})
    row = {n: i for i, n in enumerate(nodes)}
    kinds = [k for k in EVENT_STYLE if any(e.kind == k for e in p.events)]
    event_id = {k: i for i, k in enumerate(EVENT_STYLE)}  # stable across runs

    out.csv(
        "G_events",
        ["elapsed_h", "timestamp", "node", "node_index", "event", "event_id", "detail"],
        [
            [
                round(e.t / 3600, 6), e.clock, hexlabel(e.node), row[e.node],
                e.kind, event_id[e.kind],
                # `,` is the CSV separator and pgfplotstable cannot un-quote.
                e.detail.replace(", ", "; "),
            ]
            for e in p.events
        ],
    )
    # Sidecars so a pgfplots rebuild can loop rows instead of hard-coding names.
    out.csv("G_event_nodes", ["node_index", "node"], [[row[n], hexlabel(n)] for n in nodes])
    out.csv("G_event_kinds", ["event_id", "event"], [[event_id[k], k] for k in kinds])

    fig, ax = plt.subplots(figsize=(9, 0.42 * len(nodes) + 2.0), layout="constrained")
    for k in kinds:
        pts = [(e.t / 3600, row[e.node]) for e in p.events if e.kind == k]
        marker, size = EVENT_STYLE[k]
        ax.scatter(*zip(*pts), marker=marker, s=size, label=k, zorder=3)
    for n in nodes:
        ax.axhline(row[n], color="0.9", linewidth=0.7, zorder=0)
    ax.set_yticks(range(len(nodes)), [hexlabel(n) for n in nodes])
    ax.set_ylim(-0.6, len(nodes) - 0.4)
    ax.set_xlabel("elapsed time (h)")
    ax.set_ylabel("node")
    ax.set_title("Gateway-observable event timeline")
    # Constrained layout reserves exactly the space the legend needs, so there
    # is no dead band between the x-axis label and the legend.
    fig.legend(
        *ax.get_legend_handles_labels(),
        loc="outside lower center",
        ncol=min(5, len(kinds)),
        frameon=False,
        fontsize=7,
    )
    out.fig("G_events", fig)


def fig_h_packets(out: Out, p: Parsed) -> None:
    """One tick per `GATEWAY data` line: when each node's packets landed."""
    nodes = sorted({d.origin for d in p.deliveries})
    row = {n: i for i, n in enumerate(nodes)}

    # A packet_id delivered twice is a lost-ack retransmit, not a new packet.
    seen: set[tuple[int, int]] = set()
    dup: list[bool] = []
    for d in p.deliveries:
        key = (d.origin, d.pid)
        dup.append(key in seen)
        seen.add(key)

    # Rows are sorted by (node_index, timestamp) rather than strict delivery
    # order so that panel J's partitionByValues("node") encounters distinct
    # node values in the same hex-sorted order panels I and F emit -- and
    # therefore in the same Grafana palette order (series i gets the same
    # colour across J/I/F).  timestamp tie-breaks within a node so a node's
    # own dot ordering on the time axis still matches chronological order.
    row_order = sorted(
        zip(p.deliveries, dup),
        key=lambda d_u: (row[d_u[0].origin], d_u[0].t),
    )

    out.csv(
        "H_packets",
        [
            "elapsed_h", "timestamp", "node", "node_index", "packet_id", "sender",
            "hops", "delay_s", "rssi_dbm", "duplicate",
        ],
        [
            [
                round(d.t / 3600, 6), d.ts, hexlabel(d.origin), row[d.origin], d.pid,
                hexlabel(d.sender), "" if d.hops is None else d.hops,
                d.delay_s, d.rssi, int(is_dup),
            ]
            for d, is_dup in row_order
        ],
    )
    # Log end = the last timestamped LINE, not the last delivery. The age of a
    # node's last packet is the gap between that packet and the end of the log;
    # emit it explicitly as "last_heard_ago_h" so downstream consumers
    # (fetch-and-plot.sh / dashboard) don't have to reconstruct it from column
    # counts. Using deliveries[-1].t here (as the original code did)
    # underestimated each node's age when the log kept emitting PDR/event
    # lines after the final delivery -- common when a node's silence is the
    # very thing the dashboard reader is trying to spot.
    log_end_h = p.last_t / 3600
    out.csv(
        "H_packet_nodes",
        [
            "node_index", "node", "packets", "duplicates",
            "first_h", "last_h", "last_heard_ago_h",
        ],
        [
            [
                row[n], hexlabel(n),
                sum(1 for d in p.deliveries if d.origin == n),
                sum(1 for d, u in zip(p.deliveries, dup) if d.origin == n and u),
                round(min(d.t for d in p.deliveries if d.origin == n) / 3600, 6),
                round(max(d.t for d in p.deliveries if d.origin == n) / 3600, 6),
                round(log_end_h - max(d.t for d in p.deliveries if d.origin == n) / 3600, 6),
            ]
            for n in nodes
        ],
    )

    fig, ax = plt.subplots(figsize=(9, 0.42 * len(nodes) + 1.8))
    for n in nodes:
        xs = [d.t / 3600 for d in p.deliveries if d.origin == n]
        ax.scatter(xs, [row[n]] * len(xs), marker="|", s=42, linewidths=0.8, zorder=3)
    dups = [(d.t / 3600, row[d.origin]) for d, u in zip(p.deliveries, dup) if u]
    if dups:
        # A ring, not a filled marker: the node colours already use up red.
        ax.scatter(
            *zip(*dups), marker="o", s=60, facecolors="none", edgecolors="black",
            linewidths=0.9, zorder=4, label="duplicate (lost-ack retransmit)",
        )
        ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1.0))
    for n in nodes:
        ax.axhline(row[n], color="0.9", linewidth=0.7, zorder=0)
    ax.set_yticks(range(len(nodes)), [hexlabel(n) for n in nodes])
    ax.set_ylim(-0.6, len(nodes) - 0.4)
    ax.set_xlabel("elapsed time (h)")
    ax.set_ylabel("node")
    ax.set_title("Packet arrivals at the gateway")
    out.fig("H_packets", fig)


# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("log", type=Path, help="gateway serial log")
    ap.add_argument(
        "--generatedcsv",
        type=Path,
        help="`node,packets` CSV of each node's persisted EEPROM packet count; "
        "enables the authoritative PDR (spec 18.1)",
    )
    ap.add_argument("--outdir", type=Path, help="default: <log dir>/plots")
    ap.add_argument("--format", default="png", choices=("png", "pdf", "svg"))
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args()

    outdir = args.outdir or args.log.parent / "plots"
    outdir.mkdir(parents=True, exist_ok=True)

    p = parse_log(args.log)
    if not p.deliveries:
        sys.exit(f"{args.log}: no `GATEWAY data` lines — is this a gateway log?")
    generated = read_generated(args.generatedcsv) if args.generatedcsv else {}

    out = Out(outdir, args.format, args.dpi)
    node_rows = pdr_by_node(p, generated)
    hop_rows, warnings = pdr_by_hop(p, generated)

    fig_a_pdr_by_node(out, node_rows)
    fig_b_pdr_by_hop(out, hop_rows)
    fig_cd_delay(out, p)
    fig_e_topology(out, p)
    fig_f_battery(out, p)
    fig_g_events(out, p)
    fig_h_packets(out, p)
    fig_i_temperature(out, p)

    span_h = p.deliveries[-1].t / 3600
    print(f"{args.log.name}: {p.lines} lines, {span_h:.2f} h, {len(p.deliveries)} deliveries, "
          f"{len(p.topo)} origins, {len(p.events)} events")
    if p.unmatched_topo:
        print(f"  note: {p.unmatched_topo} TOPO lines had no adjacent Data line")
    dups = sum(r["duplicates"] for r in node_rows)
    if dups:
        print(f"  note: {dups} duplicate deliveries (lost-ack retransmits), excluded from PDR")
    for w in warnings:
        print(f"  warning: {w}")
    print(f"wrote {len(out.written)} files to {outdir}/")
    # Machine-readable line for fetch-and-plot.sh: the ISO timestamp of the
    # last timestamped *line* in the log (not just the last delivery), surfaced
    # for the dashboard's "last line of log time" indicator.
    print(f"last_line_ts={p.last_line_ts}")


if __name__ == "__main__":
    main()
