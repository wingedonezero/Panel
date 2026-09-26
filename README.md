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
- **Containers** — the whole Docker fleet with running/stopped dots, plus
  optional "update available" badges fed by [Diun](https://crazymax.dev/diun/)
  (see below).

Plus two opt-in extras that stay out of the way of the status page:

- **Backups tab** — manual, per-app backups (SQLite-safe database copies and
  full `.tar.gz` archives) with the last backup time of each kind at a glance.
- **One-click update** for standalone containers (the ⬆ button) — shows every
  step and Docker API call before running, streams a live log, and rolls back
  automatically if the new container doesn't stay up.

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
      # - /path/to/backups:/backups    # only needed for the Backups tab
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
| `PANEL_BACKUP_DIR` | `/backups` | Where the Backups tab writes (mount a writable volume) |
| `PANEL_BACKUP_OWNER` | — | `uid:gid` to own backup files/folders (settings page wins) |
| `PANEL_UPDATE_ALLOW` | — | Containers offered for one-click update: `name,name` (settings page wins) |

### SMART warnings banner (optional)

Panel can show smartd health warnings as a dismissible red banner. Debian 13+
has no plaintext syslog, so the recipe is a smartd exec hook: a two-line shell
script (e.g. `/usr/local/bin/panel-smart-alert`) that appends
`$(date -Iseconds)|$SMARTD_DEVICE|$SMARTD_MESSAGE` to `smart-alerts.log` in
Panel's config dir, registered with smartd via
`-m <nomailer> -M exec /usr/local/bin/panel-smart-alert` in its DEFAULT
directives. Dismissing the banner stores a watermark; only warnings newer than
the last dismissal are shown, so new problems always re-trigger.

### Update badges via Diun (optional)

Panel never checks registries itself (images are only pulled when you run a
one-click update). For "update available" badges, run
[Diun](https://crazymax.dev/diun/) and point its webhook notifier at Panel:

```yaml
environment:
  - DIUN_PROVIDERS_DOCKER=true
  - DIUN_PROVIDERS_DOCKER_WATCHBYDEFAULT=true
  - DIUN_NOTIF_WEBHOOK_ENDPOINT=http://<panel-host>:8763/api/updates/diun
  - DIUN_NOTIF_WEBHOOK_METHOD=POST
```

When Diun reports a newer image, the matching containers get an orange ⬆ in
the Containers panel. Panel records which image IDs were current at that
moment, so the badge clears itself as soon as the container is recreated on
the new image — nothing to acknowledge. State lives in `updates.json` in the
config dir.

### Backups tab (optional)

Mount a writable folder at `/backups` (see the example compose). Panel's view
of your data stays read-only — only the destination needs write access.

Add a job per app with **+ Add backup**: a data folder (host path, as Panel
sees it through its `/srv` or `/hostroot` mounts), an optional SQLite database
file inside it, which files a full backup includes, a destination subfolder
inside `/backups`, and how many backups of each kind to keep. Presets fill in
the details for known apps (e.g. Vaultwarden: `db.sqlite3` plus
`attachments`, `sends`, `config.json`, `rsa_key*`).

Each job has up to two buttons:

- **Database** — a consistent copy of the SQLite file using SQLite's online
  backup API (safe while the app keeps running), integrity-checked, saved as
  `<job>_db_<YYYY-MM-DD_HHMMSS>.sqlite3` — restore by copying it into place.
- **Full** — `<job>_full_<timestamp>.tar.gz` of the included files, with the
  database added as a consistent copy rather than the live file.

The tab shows when each kind last ran (read from the files themselves), how
many are kept and their total size, and a log for every run. Backups are
manual — there is no scheduler — and restoring is done by hand. Jobs are stored
in `backups.json` in the config dir; run logs in `logs/`. Set a file owner
(`uid:gid`) in Settings if the backup folder is shared over NFS/SMB.

### One-click update (optional)

For containers created with `docker run` (not compose/stack-managed ones —
redeploy those from their stack tool). Tick them under **Settings → One-click
update**; the ⬆ button then appears in the header. Panel refuses to update its
own container.

Clicking **Update…** first shows the full plan: every step with the
equivalent `docker` command and the exact Docker API call, plus the complete
container config that will be created. Running it then:

1. pulls the image and stops if the ID is unchanged (nothing is touched),
2. stops the container and renames it aside (`<name>-panel-old`),
3. creates an identical container on the new image — settings that came from
   the *old image* (ENV, labels, CMD, ...) are dropped so the new image's
   defaults apply; anonymous volumes are re-attached by name; extra networks
   and aliases are reconnected,
4. starts it and checks it stays running (and healthy, if it has a health
   check) — a crash loop counts as failure,
5. removes the old container, and the old image if it is untagged and unused.

If anything fails after the stop, the new container is removed and the old one
is renamed back and started again. The whole run streams live into the dialog
and is saved under `logs/`.

This uses the Docker socket that Panel already mounts. Note that `:ro` on a
socket mount does not restrict the Docker API — anything with the socket can
manage containers — so only expose Panel where you would expose Docker itself.

### storcli note

The Broadcom `storcli64` binary is not redistributed here (licensing). If you
have an LSI/Broadcom HBA, drop the binary somewhere, mount it into the
container, and point `PANEL_STORCLI` at it.

## Why not Netdata/Beszel/Grafana?

Those are monitoring platforms — history, agents, alerting, dashboards.
Panel is the other thing: the page you glance at to answer "which disk is
being written to right now, and is anything hot?" If you want graphs over
time, run one of those alongside; they coexist fine.
