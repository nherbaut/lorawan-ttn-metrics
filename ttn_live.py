#!/usr/bin/env python3
"""Live TTN gateway monitor with radio summaries (Python 3.9+, stdlib only).

Live:
  export TTN_API_KEY=...  # Prefer: read -rsp 'TTN key: ' TTN_API_KEY; export TTN_API_KEY
  python3 ttn_live.py --gateway backboard2062 --summary 60

Replay a TTN Console Gateway Live Data JSON export, without an API key:
  python3 ttn_live.py --replay backboard2062_live_data_*.json --no-events

Save complete live events as JSONL while keeping human-readable terminal output:
  python3 ttn_live.py --jsonl events.jsonl

Expose Prometheus metrics at http://HOST:9877/metrics (cumulative since startup):
  python3 ttn_live.py --no-events --summary 0 --metrics-port 9877 --metrics-host 0.0.0.0
"""
import argparse
import base64
from collections import Counter, defaultdict, deque
from datetime import datetime
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

SNR_MIN = {7: -7.5, 8: -10, 9: -12.5, 10: -15, 11: -17.5, 12: -20}
UP_TYPES = {"CONFIRMED_UP", "UNCONFIRMED_UP"}


def parse_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (AttributeError, ValueError):
        return None


def fmt(value, suffix="", decimals=None):
    if value is None:
        return "-"
    if decimals is not None:
        return f"{float(value):.{decimals}f}{suffix}"
    return f"{value}{suffix}"


def mhdr_type(encoded):
    try:
        header = base64.b64decode(encoded, validate=True)[0]
        return {
            0: "JOIN_REQUEST", 1: "JOIN_ACCEPT", 2: "UNCONFIRMED_UP",
            3: "UNCONFIRMED_DOWN", 4: "CONFIRMED_UP", 5: "CONFIRMED_DOWN",
            6: "REJOIN_REQUEST", 7: "PROPRIETARY",
        }.get(header >> 5, "UNKNOWN")
    except (ValueError, IndexError, TypeError):
        return "UNKNOWN"


def freq_mhz(value):
    try:
        return f"{float(value) / 1e6:.1f}MHz"
    except (TypeError, ValueError):
        return "-"


def event_class(event):
    name = event.get("name", "")
    data = event.get("data") or {}
    if name == "gs.up.receive":
        msg = data.get("message") or {}
        payload = msg.get("payload") or {}
        join = payload.get("join_request_payload")
        mac = payload.get("mac_payload") or {}
        fhdr = mac.get("f_hdr") or {}
        settings = msg.get("settings") or {}
        lora = (settings.get("data_rate") or {}).get("lora") or {}
        rx_list = msg.get("rx_metadata") or []
        rx = rx_list[0] if rx_list and isinstance(rx_list[0], dict) else {}
        kind = (payload.get("m_hdr") or {}).get("m_type") or (
            "JOIN_REQUEST" if join is not None else mhdr_type(msg.get("raw_payload")))
        raw = msg.get("raw_payload") or ""
        fcnt = fhdr.get("f_cnt")
        if fcnt is None and kind in UP_TYPES:
            try:
                frame = base64.b64decode(raw, validate=True)
                if len(frame) >= 8:
                    fcnt = int.from_bytes(frame[6:8], "little")
            except ValueError:
                pass
        sf, snr = lora.get("spreading_factor"), rx.get("snr")
        margin = snr - SNR_MIN[sf] if snr is not None and sf in SNR_MIN else None
        dev = (join or {}).get("dev_eui") if join is not None else fhdr.get("dev_addr")
        devtype = "DevEUI" if join is not None else "DevAddr"
        return {"category": "up", "kind": kind, "device": dev, "devtype": devtype,
                "fcnt": fcnt, "raw": raw, "sf": sf, "rssi": rx.get("rssi"),
                "snr": snr, "margin": margin, "frequency": settings.get("frequency"),
                "join_eui": (join or {}).get("join_eui"), "crc_ok": msg.get("crc_status")}
    if name == "gs.down.send":
        sched = data.get("scheduled") or {}
        lora = (sched.get("data_rate") or {}).get("lora") or {}
        ids = event.get("correlation_ids") or data.get("correlation_ids") or []
        source = "PacketBroker" if any(x.startswith("pba:") for x in ids) else "other/unknown"
        return {"category": "down", "kind": mhdr_type(data.get("raw_payload")),
                "sf": lora.get("spreading_factor"), "frequency": sched.get("frequency"),
                "power": (sched.get("downlink") or {}).get("tx_power"), "source": source}
    if name == "gs.status.receive":
        return {"category": "status", "metrics": data.get("metrics") or {}}
    return {"category": "other"}


class Statistics:
    def __init__(self):
        self.events = Counter()
        self.types = Counter()
        self.sf = Counter()
        self.freq = Counter()
        self.devices = Counter()
        self.join_euis = Counter()
        self.down_sources = Counter()
        self.rssi = []
        self.snr = []
        self.margin = []
        self.device_rssi = defaultdict(list)
        self.repeated_frames = 0
        self.fcnt_gaps = 0
        self.status = []

    def add(self, event, info, repeat=False, gap=False):
        self.events[event.get("name", "unknown")] += 1
        cat = info["category"]
        if cat == "up":
            self.types[info["kind"]] += 1
            if info["sf"] is not None:
                self.sf[info["sf"]] += 1
            if info["frequency"] is not None:
                self.freq[freq_mhz(info["frequency"])] += 1
            if info["device"]:
                key = f"{info['devtype']}:{info['device']}"
                self.devices[key] += 1
                if info["rssi"] is not None:
                    self.device_rssi[key].append(info["rssi"])
            if info["join_eui"]:
                self.join_euis[info["join_eui"]] += 1
            for name in ("rssi", "snr", "margin"):
                val = info[name]
                if val is not None:
                    getattr(self, name).append(val)
            self.repeated_frames += int(repeat)
            self.fcnt_gaps += int(gap)
        elif cat == "down":
            self.down_sources[info["source"]] += 1
            self.types[info["kind"]] += 1
        elif cat == "status":
            self.status.append(info["metrics"])


def med_summary(values, unit=""):
    if not values:
        return "n/a"
    return f"median={statistics.median(values):.1f}{unit}, range=[{min(values):g},{max(values):g}]{unit}"


def pretty_counter(counter, limit=None):
    items = counter.most_common(limit)
    return ", ".join(f"{k}:{v}" for k, v in items) if items else "none"


def status_stat(status, key):
    vals = [m[key] for m in status if m.get(key) is not None]
    return sum(vals) if vals else None


def summary_lines(stats, top, caption):
    n_up = stats.events["gs.up.receive"]
    n_down = stats.events["gs.down.send"]
    lines = [f"\n=== {caption} ===",
             f"Events={sum(stats.events.values())}  uplinks={n_up}  "
             f"downlink-commands={n_down}  status-reports={len(stats.status)}",
             f"Uplink types: {pretty_counter(Counter({k: v for k, v in stats.types.items() if 'UP' in k or k == 'JOIN_REQUEST'}))}",
             f"SF: {pretty_counter(stats.sf)}",
             f"Channels: {pretty_counter(stats.freq)}",
             f"RSSI: {med_summary(stats.rssi, 'dBm')}",
             f"SNR:  {med_summary(stats.snr, 'dB')}",
             f"Nominal SNR margin: {med_summary(stats.margin, 'dB')}",
             f"Margin <3dB: {sum(m < 3 for m in stats.margin)}/{len(stats.margin)} decoded frames (approximate thresholds)",
             f"Distinct IDs={len(stats.devices)} (DevAddr and DevEUI are separate identities), "
             f"exact-frame repeats={stats.repeated_frames}, FCnt gap observations={stats.fcnt_gaps}",
             f"Downlinks: {pretty_counter(stats.down_sources)}; types: "
             f"{pretty_counter(Counter({k: v for k, v in stats.types.items() if 'DOWN' in k or k == 'JOIN_ACCEPT'}))}"]
    if stats.status:
        rxin = status_stat(stats.status, "rxin")
        rxok = status_stat(stats.status, "rxok")
        rxfw = status_stat(stats.status, "rxfw")
        txin = status_stat(stats.status, "txin")
        txok = status_stat(stats.status, "txok")
        acks = [s["ackr"] for s in stats.status if s.get("ackr") is not None]
        lines.append(f"Gateway interval counters: rxin={fmt(rxin)} rxok={fmt(rxok)} "
                     f"rxfw={fmt(rxfw)} txin={fmt(txin)} txok={fmt(txok)}")
        if rxin:
            lines.append(f"  CRC success among detected packets={100 * (rxok or 0) / rxin:.1f}%")
        if rxok:
            lines.append(f"  Forwarded among CRC-valid packets={100 * (rxfw or 0) / rxok:.1f}%")
        if acks:
            lines.append(f"  UDP PUSH_ACK reporting: min={min(acks):g}%, avg={statistics.mean(acks):.1f}% "
                         f"({len(acks)} reports; not an uplink delivery percentage)")
    if stats.devices and top:
        lines.append(f"Most frequently observed device IDs (top {top}):")
        for key, count in stats.devices.most_common(top):
            readings = stats.device_rssi.get(key, [])
            median = f"{statistics.median(readings):g}dBm" if readings else "n/a"
            lines.append(f"  {key:<29} {count:>5} packets   RSSI median {median}")
    if stats.join_euis:
        lines.append(f"JoinEUI counts: {pretty_counter(stats.join_euis, top)}")
    lines.append("Note: event stream samples and gateway interval counters use different scopes;")
    lines.append("      FCnt gaps/repeated frames are indicators, not verified packet losses.")
    return lines


# ---------------------------------------------------------------------------
# Prometheus exposition (text format 0.0.4), stdlib only.
# ---------------------------------------------------------------------------

RSSI_BUCKETS = (-130, -125, -120, -115, -110, -105, -100, -95, -90, -80, -70, -60, -50, -30)
SNR_BUCKETS = (-20, -17.5, -15, -12.5, -10, -7.5, -5, -2.5, 0, 2.5, 5, 7.5, 10, 12.5, 15)
MARGIN_BUCKETS = (-5, -2.5, 0, 1, 2, 3, 5, 7.5, 10, 15, 20, 25, 30)
STATUS_COUNTERS = {
    "rxin": "Packets received by the gateway radio (gateway status report counter)",
    "rxok": "Received packets with a valid CRC (gateway status report counter)",
    "rxfw": "Received packets forwarded upstream (gateway status report counter)",
    "txin": "Downlink packets received for transmission (gateway status report counter)",
    "txok": "Downlink packets emitted (gateway status report counter)",
}


def _escape_label(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(pairs):
    if not pairs:
        return ""
    return "{" + ",".join(f'{k}="{_escape_label(v)}"' for k, v in pairs) + "}"


def _num(value):
    if value == float("inf"):
        return "+Inf"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value) if isinstance(value, float) else str(value)


class Histogram:
    def __init__(self, buckets):
        self.bounds = tuple(buckets) + (float("inf"),)
        self.counts = [0] * len(self.bounds)
        self.sum = 0.0
        self.count = 0

    def observe(self, value):
        value = float(value)
        for i, bound in enumerate(self.bounds):
            if value <= bound:
                self.counts[i] += 1
                break
        self.sum += value
        self.count += 1


class PrometheusMetrics:
    """Cumulative, since-startup metrics for one gateway.

    Counters only increase for the lifetime of the process; Prometheus
    handles process restarts through counter-reset detection in rate().
    """

    def __init__(self, gateway, device_labels=False):
        self.base = (("gateway_id", gateway),)
        self.device_labels = device_labels
        self.counters = defaultdict(lambda: defaultdict(float))   # name -> labels -> value
        self.gauges = defaultdict(dict)                           # name -> labels -> value
        self.histograms = {}                                      # name -> labels -> Histogram
        self.help = {}
        self.devices = set()
        self.start_time = time.time()
        self._declare()

    def _declare(self):
        h = self.help
        h["ttn_gateway_events_total"] = ("counter", "TTN events received from the Events API, by event name")
        h["ttn_gateway_uplinks_total"] = ("counter", "Uplink frames received by the gateway, by message type, spreading factor and frequency")
        h["ttn_gateway_downlinks_total"] = ("counter", "Downlink transmissions scheduled on the gateway, by message type, spreading factor and source")
        h["ttn_gateway_uplink_repeats_total"] = ("counter", "Uplinks whose raw payload repeats a frame from the same device within 1-600 s (retransmission indicator)")
        h["ttn_gateway_uplink_fcnt_gaps_total"] = ("counter", "Uplinks whose frame counter skipped values since the previous frame of the same device (loss indicator, not verified loss)")
        h["ttn_gateway_uplink_rssi_dbm"] = ("histogram", "RSSI of received uplinks in dBm")
        h["ttn_gateway_uplink_snr_db"] = ("histogram", "SNR of received uplinks in dB")
        h["ttn_gateway_uplink_snr_margin_db"] = ("histogram", "SNR minus the nominal demodulation floor for the spreading factor, in dB (approximate)")
        h["ttn_gateway_uplink_last_rssi_dbm"] = ("gauge", "RSSI of the most recent uplink in dBm")
        h["ttn_gateway_uplink_last_snr_db"] = ("gauge", "SNR of the most recent uplink in dB")
        h["ttn_gateway_distinct_devices"] = ("gauge", "Distinct DevAddr/DevEUI identities observed since startup")
        for key, text in STATUS_COUNTERS.items():
            h[f"ttn_gateway_status_{key}_total"] = ("counter", text)
        h["ttn_gateway_status_reports_total"] = ("counter", "Gateway status reports received")
        h["ttn_gateway_status_ack_ratio_percent"] = ("gauge", "UDP PUSH_ACK ratio from the latest status report (not an uplink delivery percentage)")
        h["ttn_gateway_last_event_timestamp_seconds"] = ("gauge", "Unix time of the most recent event, by category (TTN event time)")
        h["ttn_gateway_device_uplinks_total"] = ("counter", "Uplinks per observed device identity (only with --metrics-device-labels)")
        h["ttn_exporter_stream_connected"] = ("gauge", "1 while the TTN Events API stream is connected")
        h["ttn_exporter_stream_reconnects_total"] = ("counter", "TTN Events API stream (re)connection attempts that failed or closed")
        h["ttn_exporter_start_time_seconds"] = ("gauge", "Unix time at which the exporter started")
        self.gauges["ttn_exporter_stream_connected"][self.base] = 0
        self.gauges["ttn_exporter_start_time_seconds"][self.base] = self.start_time
        self.counters["ttn_exporter_stream_reconnects_total"][self.base] += 0

    def _observe(self, name, buckets, value, labels):
        hists = self.histograms.setdefault(name, {})
        hist = hists.get(labels)
        if hist is None:
            hist = hists[labels] = Histogram(buckets)
        hist.observe(value)

    def record(self, event, info, repeat, gap):
        base = self.base
        name = event.get("name", "unknown")
        self.counters["ttn_gateway_events_total"][base + (("event", name),)] += 1
        cat = info["category"]
        t = parse_time(event.get("time"))
        if t:
            self.gauges["ttn_gateway_last_event_timestamp_seconds"][base + (("category", cat),)] = t.timestamp()
        if cat == "up":
            sf = "" if info["sf"] is None else str(info["sf"])
            freq = "" if info["frequency"] is None else f"{float(info['frequency']) / 1e6:.1f}"
            self.counters["ttn_gateway_uplinks_total"][
                base + (("type", info["kind"]), ("sf", sf), ("frequency_mhz", freq))] += 1
            self.counters["ttn_gateway_uplink_repeats_total"][base] += int(repeat)
            self.counters["ttn_gateway_uplink_fcnt_gaps_total"][base] += int(gap)
            sf_labels = base + (("sf", sf),)
            if info["rssi"] is not None:
                self._observe("ttn_gateway_uplink_rssi_dbm", RSSI_BUCKETS, info["rssi"], sf_labels)
                self.gauges["ttn_gateway_uplink_last_rssi_dbm"][base] = float(info["rssi"])
            if info["snr"] is not None:
                self._observe("ttn_gateway_uplink_snr_db", SNR_BUCKETS, info["snr"], sf_labels)
                self.gauges["ttn_gateway_uplink_last_snr_db"][base] = float(info["snr"])
            if info["margin"] is not None:
                self._observe("ttn_gateway_uplink_snr_margin_db", MARGIN_BUCKETS, info["margin"], sf_labels)
            if info["device"]:
                ident = f"{info['devtype']}:{info['device']}"
                self.devices.add(ident)
                self.gauges["ttn_gateway_distinct_devices"][base] = len(self.devices)
                if self.device_labels:
                    self.counters["ttn_gateway_device_uplinks_total"][
                        base + (("device_type", info["devtype"]), ("device_id", info["device"]))] += 1
        elif cat == "down":
            sf = "" if info["sf"] is None else str(info["sf"])
            self.counters["ttn_gateway_downlinks_total"][
                base + (("type", info["kind"]), ("sf", sf), ("source", info["source"]))] += 1
        elif cat == "status":
            m = info["metrics"]
            self.counters["ttn_gateway_status_reports_total"][base] += 1
            for key in STATUS_COUNTERS:
                val = m.get(key)
                if isinstance(val, (int, float)) and val >= 0:
                    self.counters[f"ttn_gateway_status_{key}_total"][base] += val
            ackr = m.get("ackr")
            if isinstance(ackr, (int, float)):
                self.gauges["ttn_gateway_status_ack_ratio_percent"][base] = float(ackr)

    def set_connected(self, connected):
        self.gauges["ttn_exporter_stream_connected"][self.base] = 1 if connected else 0

    def count_reconnect(self):
        self.counters["ttn_exporter_stream_reconnects_total"][self.base] += 1

    def render(self):
        out = []
        for name, (kind, text) in self.help.items():
            if kind == "counter":
                series = self.counters.get(name)
            elif kind == "gauge":
                series = self.gauges.get(name)
            else:
                series = self.histograms.get(name)
            if not series:
                continue
            out.append(f"# HELP {name} {text}")
            out.append(f"# TYPE {name} {kind}")
            for labels in sorted(series):
                value = series[labels]
                if kind != "histogram":
                    out.append(f"{name}{_labels(labels)} {_num(float(value))}")
                    continue
                cumulative = 0
                for bound, count in zip(value.bounds, value.counts):
                    cumulative += count
                    out.append(f"{name}_bucket{_labels(labels + (('le', _num(float(bound))),))} {cumulative}")
                out.append(f"{name}_sum{_labels(labels)} {_num(value.sum)}")
                out.append(f"{name}_count{_labels(labels)} {value.count}")
        return "\n".join(out) + "\n"


def start_metrics_server(host, port, monitor):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/metrics":
                with monitor.lock:
                    body = monitor.metrics.render().encode("utf-8")
                ctype = "text/plain; version=0.0.4; charset=utf-8"
                code = 200
            elif path in ("/", "/healthz"):
                body, ctype, code = b"ttn_live exporter: see /metrics\n", "text/plain; charset=utf-8", 200
            else:
                body, ctype, code = b"not found\n", "text/plain; charset=utf-8", 404
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass  # keep the terminal output for TTN events

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Prometheus metrics on http://{host}:{server.server_address[1]}/metrics",
          file=sys.stderr, flush=True)
    return server


class Monitor:
    def __init__(self, args, output_file=None):
        self.args = args
        self.zone = ZoneInfo(args.timezone)
        self.out = output_file
        self.lock = threading.RLock()
        self.total = Statistics()
        self.window = Statistics()
        self.last_raw = {}
        self.last_fcnt = {}
        self.seen_order = deque()
        self.seen_ids = set()
        self.dedup_size = 30000
        self.ansi = args.color == "always" or (args.color == "auto" and sys.stdout.isatty())
        self.stop_event = threading.Event()
        self.metrics = PrometheusMetrics(args.gateway, args.metrics_device_labels)

    def _write(self, value, dest=sys.stdout):
        print(value, file=dest, flush=True)

    def _paint(self, value, color):
        return f"\033[{color}m{value}\033[0m" if self.ansi else value

    def display(self, event, info, repeat, gap):
        if self.args.no_events:
            return
        if self.args.raw:
            self._write(json.dumps(event, ensure_ascii=False))
            return
        t = parse_time(event.get("time"))
        time_label = t.astimezone(self.zone).strftime("%H:%M:%S") if t else "??:??:??"
        cat = info["category"]
        if cat == "up":
            kind = info["kind"]
            color = "36" if kind == "JOIN_REQUEST" else "32" if kind == "UNCONFIRMED_UP" else "37"
            ident = f"{info['devtype']}={info['device'] or '-'}"
            if info["fcnt"] is not None:
                ident += f" FCnt={info['fcnt']}"
            extra = " RETRY?" if repeat else ""
            if gap:
                extra += " FCNT-GAP"
            self._write(f"{time_label} {self._paint('UP  ', color)} {kind:<16} {ident:<38} "
                        f"SF{fmt(info['sf']):<2} {freq_mhz(info['frequency']):<9} "
                        f"RSSI={fmt(info['rssi'], 'dBm'):<8} "
                        f"SNR={fmt(info['snr'], 'dB'):<8} "
                        f"margin={fmt(info['margin'], 'dB')}{extra}")
        elif cat == "down":
            self._write(f"{time_label} {self._paint('DOWN', '33')} "
                        f"{info['kind']:<16} SF{fmt(info['sf']):<2} "
                        f"{freq_mhz(info['frequency'])} TX={fmt(info['power'], 'dBm')} "
                        f"via={info['source']}")
        elif cat == "status":
            if self.args.quiet_status:
                return
            m = info["metrics"]
            values = " ".join(f"{k}={fmt(m.get(k))}" for k in
                              ("rxin", "rxok", "rxfw", "ackr", "txin", "txok"))
            self._write(f"{time_label} {self._paint('STAT', '34')} {values}")
        else:
            self._write(f"{time_label} {self._paint('EVT ', '35')} {event.get('name', 'unknown')}")

    def handle(self, event):
        if not isinstance(event, dict) or "name" not in event:
            return
        with self.lock:
            unique_id = event.get("unique_id")
            if unique_id:
                if unique_id in self.seen_ids:
                    return
                if len(self.seen_order) >= self.dedup_size:
                    self.seen_ids.discard(self.seen_order.popleft())
                self.seen_order.append(unique_id)
                self.seen_ids.add(unique_id)
            info = event_class(event)
            repeat = gap = False
            if info["category"] == "up":
                ident = (info["devtype"], info["device"])
                t = parse_time(event.get("time"))
                raw = info.get("raw")
                if raw:
                    key = (ident, raw)
                    prev = self.last_raw.get(key)
                    if prev and t and 1 <= (t - prev).total_seconds() <= 600:
                        repeat = True
                    self.last_raw[key] = t
                fcnt = info.get("fcnt")
                if info["kind"] in UP_TYPES and info["device"] and fcnt is not None:
                    prev = self.last_fcnt.get(ident)
                    if prev is not None:
                        delta = (fcnt - prev) % 65536
                        gap = 1 < delta < 32768
                    self.last_fcnt[ident] = fcnt
            self.window.add(event, info, repeat, gap)
            self.total.add(event, info, repeat, gap)
            self.metrics.record(event, info, repeat, gap)
            if self.out:
                self.out.write(json.dumps(event, ensure_ascii=False) + "\n")
                self.out.flush()
            self.display(event, info, repeat, gap)

    def print_summary(self, total=False, force=False):
        with self.lock:
            stats = self.total if total else self.window
            if not force and not stats.events:
                return
            dest = sys.stderr if self.args.raw else sys.stdout
            label = "TOTAL since startup" if total else f"LAST {self.args.summary:g} SECONDS"
            for line in summary_lines(stats, self.args.top, label):
                self._write(line, dest)
            if not total:
                self.window = Statistics()

    def summary_worker(self):
        while self.args.summary > 0 and not self.stop_event.wait(self.args.summary):
            self.print_summary()


def parse_stream(response, callback):
    """Accept TTN newline-delimited JSON and conventional SSE 'data:' records."""
    data_lines = []
    for line in response:
        s = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not s:
            if data_lines:
                decode_event("\n".join(data_lines), callback)
                data_lines.clear()
        elif s.startswith("data:"):
            fragment = s[5:].lstrip(" ")
            # Handle both SSE records (terminated by a blank line) and
            # TTN-style newline-delimited single-line JSON records.
            try:
                complete = json.loads(fragment) if not data_lines else None
            except json.JSONDecodeError:
                complete = None
            if isinstance(complete, dict):
                decode_event(fragment, callback)
            else:
                data_lines.append(fragment)
        elif s.lstrip().startswith("{"):
            if data_lines:
                decode_event("\n".join(data_lines), callback)
                data_lines.clear()
            decode_event(s, callback)
        # Ignore SSE comments / event / id / retry fields.
    if data_lines:
        decode_event("\n".join(data_lines), callback)


def decode_event(payload, callback):
    try:
        item = json.loads(payload)
    except json.JSONDecodeError:
        print("WARNING: malformed event JSON ignored", file=sys.stderr, flush=True)
        return
    if isinstance(item, dict) and "error" in item:
        print(f"TTN stream error: {item['error']}", file=sys.stderr, flush=True)
        return
    item = item.get("result", item) if isinstance(item, dict) else item
    if isinstance(item, dict):
        callback(item)


def connect(args, key, callback, on_connect=None):
    reqbody = {"identifiers": [{"gateway_ids": {"gateway_id": args.gateway}}]}
    if args.tail:
        reqbody["tail"] = args.tail
    if args.name:
        reqbody["names"] = args.name
    request = urllib.request.Request(
        args.server.rstrip("/") + "/api/v3/events",
        data=json.dumps(reqbody).encode(), method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Accept": "text/event-stream", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        print("Connected to TTN Events API", file=sys.stderr, flush=True)
        if on_connect:
            on_connect()
        parse_stream(response, callback)


def replay_files(paths, callback):
    events = []
    for path in paths:
        data = Path(path).read_text(encoding="utf-8-sig")
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            obj = [json.loads(line) for line in data.splitlines() if line.strip()]
        if isinstance(obj, dict):
            obj = obj.get("events", [obj])
        if not isinstance(obj, list):
            raise ValueError(f"{path}: expected a JSON array or JSONL events")
        events.extend(e.get("result", e) for e in obj if isinstance(e, dict))
    # TTN Console exports are newest-first; chronological order is required
    # for meaningful frame-counter progression and retry detection.
    events.sort(key=lambda e: e.get("time", ""))
    for event in events:
        callback(event)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", default=os.environ.get("TTN_GATEWAY_ID", "backboard2062"),
                   help="TTN gateway ID (env TTN_GATEWAY_ID)")
    p.add_argument("--server", default=os.environ.get("TTN_SERVER", "https://eu1.cloud.thethings.network"),
                   help="TTN cluster URL (env TTN_SERVER)")
    p.add_argument("--timezone", default=os.environ.get("TTN_TIMEZONE", "Europe/Paris"))
    p.add_argument("--tail", type=int, default=0, help="request recent events, if supported")
    p.add_argument("--name", action="append", default=[], help="server-side event filter; repeatable")
    p.add_argument("--summary", type=float, default=60, help="periodic summary interval in seconds (0 disables)")
    p.add_argument("--top", type=int, default=5, help="number of device IDs shown in summaries")
    p.add_argument("--quiet-status", action="store_true", help="include status metrics in summaries only")
    p.add_argument("--no-events", action="store_true", help="periodic summaries only")
    p.add_argument("--raw", action="store_true", help="output machine-readable JSONL events to stdout")
    p.add_argument("--jsonl", type=Path, help="also append every event to this JSONL file")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    p.add_argument("--replay", nargs="+", metavar="JSON", help="analyse saved TTN Console exports offline")
    p.add_argument("--metrics-port", type=int, default=0,
                   help="serve Prometheus metrics on this port at /metrics (0 disables; e.g. 9877)")
    p.add_argument("--metrics-host", default="127.0.0.1",
                   help="address for the metrics server (use 0.0.0.0 for a remote Prometheus)")
    p.add_argument("--metrics-device-labels", action="store_true",
                   help="also export per-device uplink counters (high cardinality; off by default)")
    args = p.parse_args()
    if not 0 <= args.metrics_port <= 65535:
        p.error("--metrics-port must be between 0 and 65535")
    if args.summary < 0 or args.top < 0 or args.tail < 0:
        p.error("--summary, --top, and --tail must be nonnegative")
    if args.no_events and args.raw:
        p.error("--no-events and --raw are mutually exclusive")
    try:
        ZoneInfo(args.timezone)
    except Exception as e:
        p.error(f"invalid --timezone: {e}")
    key = os.environ.get("TTN_API_KEY", "").strip()
    if not args.replay and not key:
        p.error("TTN_API_KEY must be set in the environment")

    out = args.jsonl.open("a", encoding="utf-8") if args.jsonl else None
    monitor = Monitor(args, out)
    server = None

    def stream_state(connected, failed=False):
        with monitor.lock:
            monitor.metrics.set_connected(connected)
            if failed:
                monitor.metrics.count_reconnect()

    try:
        if args.metrics_port:
            try:
                server = start_metrics_server(args.metrics_host, args.metrics_port, monitor)
            except OSError as exc:
                p.error(f"cannot start metrics server on {args.metrics_host}:{args.metrics_port}: {exc}")
        if args.replay:
            replay_files(args.replay, monitor.handle)
            monitor.print_summary(total=True, force=True)
            if server:
                print("Replay finished; serving metrics until interrupted (Ctrl-C)",
                      file=sys.stderr, flush=True)
                monitor.stop_event.wait()
        else:
            thread = threading.Thread(target=monitor.summary_worker, daemon=True)
            thread.start()
            delay = 3
            while True:
                try:
                    connect(args, key, monitor.handle, on_connect=lambda: stream_state(True))
                    print("Stream closed; reconnecting", file=sys.stderr, flush=True)
                    delay = 3
                except urllib.error.HTTPError as exc:
                    msg = exc.read(2048).decode("utf-8", errors="replace")
                    print(f"HTTP {exc.code}: {msg}", file=sys.stderr, flush=True)
                    if exc.code in (400, 401, 403, 404):
                        return 1
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    print(f"Stream interrupted: {exc}", file=sys.stderr, flush=True)
                stream_state(False, failed=True)
                time.sleep(delay)
                delay = min(delay * 2, 60)
    except KeyboardInterrupt:
        print("\nStopped", file=sys.stderr, flush=True)
    finally:
        monitor.stop_event.set()
        if server:
            server.shutdown()
        if not args.replay:
            monitor.print_summary(total=True)
        if out:
            out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
