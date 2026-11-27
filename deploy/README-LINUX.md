# Miner Hearth on Linux: headless web edition (v1.17.1)

The Windows app is a window around a small web server. On Linux (a server, a Raspberry Pi, a NAS, a
5tratumOS or Umbrel box) Miner Hearth runs **headless**: only the web dashboard, opened from any browser.
It needs Python 3.10+ and nothing else (standard library only), or Docker.

## Run it directly

    python3 miner_dashboard.py --headless --host 0.0.0.0 --port 8080

- `--headless` (or `MINER_HEARTH_HEADLESS=1`): no window, no pop-ups; runs until Ctrl+C / SIGTERM.
  Linux without a display (`DISPLAY` / `WAYLAND_DISPLAY` unset) is headless automatically.
- `--host` (`MINER_HEARTH_HOST`): address to listen on. The default is `127.0.0.1` (this computer only).
- `--port` (`MINER_HEARTH_PORT`): default 8080 (the next free port if it is taken).
- `MINER_HEARTH_HOME`: the data folder (config.json, userdata/, logs/). Default: the program folder.
- `--trusted-networks` (`MINER_HEARTH_TRUSTED_NETWORKS`): comma-separated networks, e.g.
  `192.168.1.10/32` or `192.168.1.0/24`, whose browsers get **full control**, like the Miner Hearth computer
  itself. Every other browser is a LAN device: read-only by default, and asked for the PIN if you set one in
  Settings → LAN access. Because those settings can only be changed with full control, trust at least the
  computer you set it up from. Only trust networks you control; anyone there can change your miners.
- `MINER_HEARTH_RESTART_BY_EXIT=1`: for a live update's Quick restart, exit with code 75 and let systemd /
  Docker start the program again, instead of starting a new copy itself.

`deploy/miner-hearth.service` is a ready systemd unit (instructions inside).

## Docker

    docker build -t miner-hearth .            # in the program folder (has the Dockerfile)
    mkdir -p data && sudo chown 1000:1000 data
    docker run -d --name miner-hearth --network host --restart unless-stopped \
      -v "$PWD/data:/data" -e MINER_HEARTH_TRUSTED_NETWORKS=192.168.1.10/32 miner-hearth

or `docker compose up -d` with `deploy/docker-compose.yml`. The image runs as user 1000, keeps everything in
the `/data` volume, has a health check on `/api/status`, and is built for amd64 and arm64 by
`.github/workflows/docker.yml` (published as `ghcr.io/miner-hearth/miner-hearth`).

- **Host networking** (`network_mode: host`) is recommended so Miner Hearth can scan your network for miners.
  With published ports (`-p 8080:8080`, Docker Desktop) add miners by IP or set the subnet to scan.
- **Node monitoring:** add your nodes in Settings → Node Monitoring. A cookie file path must be inside the
  container (mount it read-only, e.g. `-v ~/.bitcoin/.cookie:/run/bitcoin.cookie:ro`).

## Moving from Windows

Copy `config.json`, `userdata/` and `logs/` from the Windows Miner Hearth folder into the data folder
(`/data` for Docker) while Miner Hearth is stopped; machine names, settings, saved pools and history carry over.
Windows-only features (app window, tray, Windows notifications, start with Windows) are not available.
