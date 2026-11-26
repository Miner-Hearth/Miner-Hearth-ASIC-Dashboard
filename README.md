# Miner Hearth

**A free dashboard and auto-tuner for home crypto miners, for Windows, Linux servers, Docker, Umbrel and 5tratumOS.** Miner Hearth keeps your miners tuned, watched and honest about the odds: it adjusts frequency and voltage on Bitaxe-class miners from their temperatures and error rate, monitors Antminers and many other ASICs, tells you when a machine has a problem, and alerts you (sound, Windows toast, Discord, Telegram or ntfy) when one of your machines finds a block. It can also watch your own Bitcoin node, Lightning node and Electrum server.

It runs entirely on your own computer. There is no account, no cloud service and no telemetry. Nothing is preset: you add your own machines with **Scan LAN**, and everything you add stays in a local `userdata` folder.

**Download:** get the latest installer or portable zip from the **[Releases page](https://github.com/<github-user>/<repo>/releases/latest)**.

**Used the program under its old name (up to v1.16.3)?** v1.17.0 is the same program; your machines and settings move over automatically. See [docs/MIGRATION.md](docs/MIGRATION.md).

Donate BTC: `bc1q3m8usp6mffv4txhwcltq9lu9jltx02eyukgrvf`

---

## Supported miners

| Family | What Miner Hearth does |
| --- | --- |
| **AxeOS / ESP-Miner**: Bitaxe (Gamma and other AxeOS models, Bitaxe Touch), NerdQAxe / NerdQAxe+ / NerdOCTAxe, QAxe, hexOS, Lucky Miner LV06–LV08 | Full monitoring and **autotune** (frequency by temperature, core voltage by hardware-error rate, emergency drop on overheating), pool changes, restart, fault clear |
| **Antminer** (stock Bitmain firmware: S19 / S21 / T21 / L7 / L9 / KS …) | Monitoring over the CGMiner API (hashboards, fans, pools, shares); reboot with a web password you save |
| **Braiins OS+** (S19, S21, Mini Miner) | Monitoring plus optional power-target management (off by default) |
| **VNish, LuxOS, Whatsminer, Avalon, Goldshell, IceRiver, Elphapex, Sealminer, FutureBit Apollo** | Monitoring; write controls are opt-in per machine |
| **Anything else with a CGMiner, Prometheus or JSON API** | Read-only generic adapters |

Tier-2 adapters (everything below Braiins in the table) were built from published API documentation and tested against simulators; reports from real hardware are welcome.

## Features

- **Autotune** for AxeOS miners: frequency steps by ASIC / VR temperature, voltage by error rate, learned stable-voltage floors, per-machine caps and targets. Off for every machine until you turn it on.
- **Dashboard**: live cards per machine, fleet summary, 24 h / 7 day / 30 day charts, fleet comparison table, details panel with every reading a miner exposes, light / dark / high-contrast themes, °C or °F, phone access over your LAN (optional PIN, read-only by default).
- **Problem alerts**: offline, hashrate drop, fallback pool, no shares, pool rejects, overheating and firmware fault flags, each with a plain-English explanation. Pool switches and restarts are recognised and don't raise false alarms.
- **Block alerts**: "Block Found #height" and "Block Confirmed #height" (confirmed on the coin's own blockchain) with payout address, worker, the winning share's difficulty and the network difficulty. Works for BTC, BCH, BSV, eCash, DigiByte (SHA-256), Fractal, CheetahCoin and more; HeliosPool's block feed is used when you mine there.
- **Node monitoring** (v1.17, off by default): Bitcoin Core / Knots (sync, height, peers, mempool, disk; alerts when it is down or falling behind), other coins' nodes with the same RPC, LND / Core Lightning (sync, channels), Fulcrum / Electrs (tip vs. your node), and the host's CPU, temperature, memory and disk. Credentials never leave the Miner Hearth computer.
- **Block odds and profit**: chance of a block per day / month / year for each coin, power cost, expected earnings, break-even electricity rate, time-of-use schedules.
- **Saved pools**: keep the pools you use and pick them when switching a machine's pool, with the machine's name filled in as the worker.
- **Full event log**: a redacted, append-only log of everything that happened, exportable as one zip that is safe to share for troubleshooting.
- **Live updates** (opt-in): apply a verified update package (SHA-256 manifest, optional Ed25519 signature) without reinstalling; roll back at any time.
- **Maintenance**: quiet-mode fan schedule, weekly or hung-miner restarts, firmware update notices, backup / restore.

## Quick start (Windows)

1. Download **MinerHearth-Setup-v1.17.0.exe** (or the portable zip) from [Releases](https://github.com/<github-user>/<repo>/releases/latest).
2. Run it. Windows SmartScreen may say "Windows protected your PC" because the program isn't code-signed: click **More info → Run anyway**. No administrator rights are needed; it installs for your Windows account only, in `%LOCALAPPDATA%\Programs\Miner Hearth`.
3. In the dashboard, click **Scan LAN** (or **Add machine**) to find your miners.
4. Autotune, webhooks, node monitoring and every feature that changes a miner start **off**: turn on what you want in **Settings**.

Windows 10 / 11 (64-bit). Your machines, settings and logs live in `userdata\` and are kept when you upgrade. See [INSTALL.txt](INSTALL.txt) and the [user guide](docs/USER_GUIDE.md) for more.

## Linux, Docker and home servers

A headless web edition serves the same dashboard to any browser:

```bash
python3 miner_dashboard.py --headless --host 0.0.0.0 --port 8080       # Python 3.10+, no packages needed
docker run -d --name miner-hearth --network host -v "$PWD/data:/data" \
  -e MINER_HEARTH_TRUSTED_NETWORKS=192.168.1.10/32 ghcr.io/<github-user>/miner-hearth:1.17.0
```

- [deploy/README-LINUX.md](deploy/README-LINUX.md): options, trusted networks, Docker Compose, systemd.
- [umbrel/README.md](umbrel/README.md): an Umbrel community app store (uses Umbrel's Bitcoin node and Electrs when present).
- [5tratumos/README.md](5tratumos/README.md): 5tratumOS Custom Store / GLOBAL submission / plain Docker.
- The image is built for amd64 and arm64 by [.github/workflows/docker.yml](.github/workflows/docker.yml) when you push a `v*` tag.

## Run or build from source

Miner Hearth is pure Python 3 standard library; no packages are needed to run it.

```bat
git clone https://github.com/<github-user>/<repo>.git
cd <repo>
python miner_dashboard.py            REM opens the dashboard at http://127.0.0.1:8080
python miner_dashboard.py --dry-run  REM watch only: never changes a miner
```

(`run.bat` and `run-dry.bat` do the same.)

**Run the tests**

```bat
python -m unittest test_autotune test_v113 test_v114 test_v1154 test_v1155 test_v1155_blocks test_v1155_chta test_v116 test_v1161 test_v1162 test_v1163 test_v117
```

`mock_server.py` simulates miners if you don't want to test against real hardware.

**Build MinerHearth.exe** (Windows, 64-bit Python 3.9+): run `build-exe.bat`. It creates a local `.venv-build`, installs the pinned build tools from `packaging/requirements-build.txt` (PyInstaller, pywebview) and builds with `packaging/MinerHearth.spec`.

**Build the installer**: install [NSIS 3](https://nsis.sourceforge.io/), put `MinerHearth.exe`, `VERSION`, `README.md`, `INSTALL.txt`, `enable-autostart.bat`, `disable-autostart.bat`, `config.json` and `packaging/miner_dashboard.ico` in one folder, then:

```bat
makensis -DVERSION=1.17.0 -DSTAGE=<that folder> -DOUTFILE=MinerHearth-Setup-v1.17.0.exe packaging\installer.nsi
```

**Live-update packages**: see `tools/make_update.py` (`genkey`, `build`).

## Privacy

- Nothing is sent anywhere except what you turn on: public block explorers and price feeds for network data, pool block lists, firmware release checks, the nodes you add, and the webhooks you configure.
- `config.json` ships with `"devices": []`. Your machines, passwords, pools, node credentials and history stay in `userdata` on your computer. Don't share that folder.
- The event-log export and diagnostics remove pool usernames, wallets, Wi-Fi names, MAC addresses, passwords and tokens, and mask IP addresses.

## Contributing

Bug reports, hardware compatibility reports and pull requests are welcome. Please never include your own wallet addresses, worker names, Wi-Fi names or IP addresses in issues or tests; use the **Export full log** button, which removes them.

## License

[MIT](LICENSE). Donations are appreciated but never required: `bc1q3m8usp6mffv4txhwcltq9lu9jltx02eyukgrvf`.
