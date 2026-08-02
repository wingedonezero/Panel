# Panel

A lightweight, Unraid-style **live status page** for a home server. One container,
one auto-refreshing page. No history, no database, no agents, no hub — it reads
`/proc`, `hwmon`, `smartctl`, `hdparm`, the Docker socket, `nvidia-smi`, and
(optionally) `storcli`, and shows you *right now*:

- **Disks** — every physical drive as a row: spin state (green = spinning,
  grey = sleeping), temperature, **live per-disk read/write speeds**, usage bar,
  free space. mergerfs pools get summary rows (auto-detected).
- **System** — CPU load + temp + package power (RAPL), memory.
- **GPU** — load, encoder sessions, VRAM, temp, power (NVIDIA; panel hides itself
  without one).
- **Network** — live LAN in/out; Tailscale shown separately when present.
- **Temps & fans** — every motherboard/chipset/NIC sensor hwmon exposes
  (bogus readings filtered), fan RPMs, optional LSI/Broadcom HBA temperature.
- **Containers** — the whole Docker fleet with running/stopped dots.

### Design choices

- **Standby-safe by design**: spin state via `hdparm -C` and temps via
  `smartctl -n standby` — the page never wakes a sleeping drive.
- **Idle pause**: when no browser has polled for ~15s, all probing stops
  (`PANEL_IDLE_PAUSE`, on by default). A page nobody is watching costs nothing.
- **Zero-config by default**: disks, mounts, pools, CPU sensor, GPU are
  discovered automatically. Env vars only add cosmetics (disk labels) or
  opt-in extras (storcli).

## Run

See [docker-compose.example.yml](docker-compose.example.yml). Short version:

```yaml
services:
  panel:
    image: ghcr.io/wingedonezero/panel:latest
    network_mode: host    # real NIC stats; UI on http://<host>:8763
    pid: host             # host mount table for auto-discovery
    privileged: true      # hdparm/smartctl on raw disks
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /dev:/dev
      - /srv:/srv:ro
      - /:/hostroot:ro
```

### Settings page

The gear (⚙) opens a settings page: title, poll interval, idle-pause toggle +
window, and per-disk labels/visibility. Saved to `/config/settings.json`
(mount a volume at `/config` to persist) — settings win over env vars, apply
live, and new disks/pools always appear automatically; labels are cosmetic.
If `/config/bin/storcli64*` exists it is auto-detected — no env needed.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PANEL_TITLE` | hostname | Name in the header |
| `PANEL_PORT` | `8763` | HTTP port |
| `PANEL_INTERVAL` | `2` | Sampling interval (seconds) |
| `PANEL_IDLE_PAUSE` | `true` | Stop probing when nobody is watching |
| `PANEL_IDLE_WINDOW` | `15` | Seconds without a viewer before pausing |
| `PANEL_DISKS` | *(auto)* | Cosmetic labels: `SERIAL=label,SERIAL=label` |
| `PANEL_HIDE_DISKS` | — | Serials to hide: `SERIAL,SERIAL` |
| `PANEL_POOLS` | *(auto: mergerfs)* | Pools: `name=/path;name2=/path2` |
| `PANEL_STORCLI` | — | Path to `storcli64` for LSI HBA temp (mount it in) |
| `PANEL_NET_LAN_REGEX` | `^(eth\|en\|bond)` | Which interfaces count as LAN |
| `PANEL_SPIN_EVERY` | `15` | Spin-state/temp probe cadence (seconds) |
| `PANEL_LSI_EVERY` | `60` | storcli probe cadence (seconds) |

### storcli note

The Broadcom `storcli64` binary is not redistributed here (licensing). If you
have an LSI/Broadcom HBA, drop the binary somewhere, mount it into the
container, and point `PANEL_STORCLI` at it.

## Why not Netdata/Beszel/Grafana?

Those are monitoring platforms — history, agents, alerting, dashboards.
Panel is the other thing: the page you glance at to answer "which disk is
being written to right now, and is anything hot?" If you want graphs over
time, run one of those alongside; they coexist fine.
