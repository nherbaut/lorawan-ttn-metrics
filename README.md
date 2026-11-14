# lorawan-ttn-metrics

`ttn_live.py` follows a gateway on The Things Network (Events API v3), prints
events and periodic summaries, and can expose the same data as Prometheus
metrics. Python 3.9+, standard library only.

## Prometheus endpoint

```bash
export TTN_API_KEY=...   # gateway key with "view gateway status" rights
python3 ttn_live.py --gateway backboard2062 --no-events --summary 0 \
    --metrics-port 9877 --metrics-host 0.0.0.0
```

- `--metrics-port` enables the HTTP server (`0`, the default, disables it).
  Endpoints: `/metrics` (exposition format 0.0.4) and `/healthz`.
- `--metrics-host` defaults to `127.0.0.1`; use `0.0.0.0` when Prometheus runs
  on another host or in a container.
- `--metrics-device-labels` adds `ttn_gateway_device_uplinks_total` with one
  series per DevAddr/DevEUI. It is off by default because a public gateway
  hears hundreds of foreign devices, which creates high cardinality.
- With `--replay`, the exporter keeps serving the replayed totals until Ctrl-C.

All counters are cumulative since process start. Use `rate()` / `increase()`;
Prometheus handles restarts through counter-reset detection.

Scrape configuration:

```yaml
scrape_configs:
  - job_name: ttn_gateway
    scrape_interval: 30s
    static_configs:
      - targets: ["ares:9877"]
```

## Docker image

The image (`python:3.12-alpine`, non-root, ~49 MB) runs the exporter on port
9877 by default. Configuration is by environment variables: `TTN_API_KEY`
(required, never baked into the image), `TTN_GATEWAY_ID` (default
`backboard2062`), `TTN_SERVER`, `TTN_TIMEZONE`. Any CLI argument can be
passed after the image name; it replaces the default command.

```bash
docker run -d --name ttn-exporter --restart unless-stopped \
  -e TTN_API_KEY -e TTN_GATEWAY_ID=backboard2062 -p 9877:9877 \
  ghcr.io/nherbaut/lorawan-ttn-metrics:latest
```

`make` targets:

| Target | Action |
|---|---|
| `make build` | local image for the host architecture |
| `make run` | run the local image (needs `TTN_API_KEY`) |
| `make multiarch` | build `linux/amd64` + `linux/arm64` without pushing |
| `make push` | build both architectures and push to `ghcr.io/<owner>/lorawan-ttn-metrics` |
| `make login` | `docker login ghcr.io` with `GHCR_TOKEN` (only for pushes from a workstation) |
| `make inspect` | show the pushed multi-arch manifest |

Variables: `OWNER` (from the git remote, else `nherbaut`), `VERSION`
(`git describe`, else `dev`), `EXTRA_TAGS` (default `latest`), `PLATFORMS`.

### Publishing from GitHub

`.github/workflows/image.yml` runs `make push` on pushes to `main`, on `v*`
tags and on manual dispatch, authenticating with the workflow's
`GITHUB_TOKEN` (`packages: write`); no secret has to be created. Pull
requests run `make multiarch` only. Tags produced: `git describe` output
(e.g. `v0.1.0`, or `v0.1.0-3-gabc1234` between releases) and `latest`.

The package is created on the first push and is private by default; make it
public under the package settings if Prometheus hosts pull it without
`docker login`.

## Metrics

Every series carries `gateway_id`.

| Metric | Type | Labels | Source |
|---|---|---|---|
| `ttn_gateway_events_total` | counter | `event` | all events by name |
| `ttn_gateway_uplinks_total` | counter | `type`, `sf`, `frequency_mhz` | `gs.up.receive` |
| `ttn_gateway_downlinks_total` | counter | `type`, `sf`, `source` | `gs.down.send` |
| `ttn_gateway_uplink_repeats_total` | counter | | same raw frame within 1–600 s (retry indicator) |
| `ttn_gateway_uplink_fcnt_gaps_total` | counter | | FCnt skips per device (loss indicator, not verified loss) |
| `ttn_gateway_uplink_rssi_dbm` | histogram | `sf` | uplink RSSI |
| `ttn_gateway_uplink_snr_db` | histogram | `sf` | uplink SNR |
| `ttn_gateway_uplink_snr_margin_db` | histogram | `sf` | SNR minus nominal SF floor (approximate) |
| `ttn_gateway_uplink_last_rssi_dbm`, `ttn_gateway_uplink_last_snr_db` | gauge | | latest uplink |
| `ttn_gateway_distinct_devices` | gauge | | distinct DevAddr/DevEUI since start |
| `ttn_gateway_status_{rxin,rxok,rxfw,txin,txok}_total` | counter | | sum of `gs.status.receive` interval counters |
| `ttn_gateway_status_reports_total` | counter | | status reports |
| `ttn_gateway_status_ack_ratio_percent` | gauge | | latest UDP PUSH_ACK ratio (not uplink delivery) |
| `ttn_gateway_last_event_timestamp_seconds` | gauge | `category` | TTN event time of latest up/down/status/other |
| `ttn_exporter_stream_connected` | gauge | | 1 while the Events API stream is open |
| `ttn_exporter_stream_reconnects_total` | counter | | stream closures and failed connections |
| `ttn_exporter_start_time_seconds` | gauge | | exporter start time |

Example PromQL:

```promql
sum by (sf) (rate(ttn_gateway_uplinks_total[5m])) * 60          # uplinks/min per SF
sum by (frequency_mhz) (increase(ttn_gateway_uplinks_total[1h])) # channel usage
histogram_quantile(0.5, sum by (le) (rate(ttn_gateway_uplink_rssi_dbm_bucket[15m])))
increase(ttn_gateway_status_rxok_total[1h]) / increase(ttn_gateway_status_rxin_total[1h])  # CRC success
time() - ttn_gateway_last_event_timestamp_seconds{category="status"}                       # staleness
```

The event-stream metrics and the gateway status counters have different
scopes (decoded frames vs. all radio detections), as in the terminal summary.
