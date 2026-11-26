# Miner Hearth v1.17.0

A free dashboard and auto-tuner for home miners (Bitaxe / NerdQAxe / NerdOCTAxe and other AxeOS miners, Antminer, Braiins OS+, and many more). Everything runs on your own computer: no account, no cloud.

**New name.** This is the program that was published as *Nonce Keeper* up to v1.16.3. In British English "nonce" is an offensive slang word, so it is now **Miner Hearth**. Same program, same data: your machines, settings, saved pools and history move over on the first start (details in `docs/MIGRATION.md`).

## Downloads

| File | What it is |
| --- | --- |
| **MinerHearth-Setup-v1.17.0.exe** | Windows installer (recommended). Installs for your Windows account only, no administrator rights needed. Also tidies up an install under the old name (keeps its data). |
| **miner-hearth-umbrel-v1.17.0.zip** | Umbrel community app store (headless web edition in Docker), with instructions. |
| **miner-hearth-5tratumos-v1.17.0.zip** | 5tratumOS: a Custom Store, a GLOBAL-catalogue submission and plain Docker Compose, with instructions. |
| **SHA256SUMS.txt** | Checksums to verify your download. |

Windows 10 / 11, 64-bit. Linux / Docker: amd64 and arm64 image `ghcr.io/<github-user>/miner-hearth:1.17.0`.

## New in v1.17.0

- **Renamed to Miner Hearth.** New program name (`MinerHearth.exe`), install folder (`%LOCALAPPDATA%\Programs\Miner Hearth`), shortcuts and Apps entry.
- **Automatic move from the old name.** On the first start Miner Hearth copies `userdata\`, `logs\` and your `config.json` from an old install or portable folder. It never overwrites anything and never changes the old folder. The installer removes the old program's files, shortcuts, sign-in entry and Apps entry, and keeps its data.
- **Node monitoring** (off by default; Settings → Node Monitoring): your Bitcoin Core / Knots node (sync, height, peers, mempool, disk; alerts when it is down or falling behind), other coins' nodes with the same RPC (eCash, CHTA, ...), LND / Core Lightning (sync, channels), Fulcrum / Electrs (tip vs. your node), and this computer's CPU, temperature, memory and disk. Passwords and macaroons stay on the Miner Hearth computer and never reach a browser.
- **Headless Linux edition and Docker.** `python3 miner_dashboard.py --headless`, a Dockerfile (amd64 / arm64, non-root, data in `/data`), Docker Compose and systemd files.
- **Umbrel and 5tratumOS packages**, using their app proxy and a persistent data folder; on Umbrel, the Bitcoin node and Electrs are picked up automatically when Umbrel provides them.

**Live updates:** an old v1.16.x program can't live-update itself to v1.17.0 (the program itself was renamed). Install this version once with the installer or the zip; later v1.17.x live updates work as before.

The full history is in `CHANGELOG.md` in the repository.

## Install (Windows)

**Installer**
1. Download `MinerHearth-Setup-v1.17.0.exe` and run it.
2. If Windows SmartScreen says **"Windows protected your PC"**, click **More info**, then **Run anyway**. This appears because the program isn't code-signed; it doesn't mean anything was found in the file. Check the checksum below first if you want to be sure.
3. Choose the options you want (Start Menu shortcut, desktop shortcut, start when you sign in; *Move from the old name* is ticked when an old install is found) and click **Install**.
4. In the dashboard, click **Scan LAN** (or **Add machine**) to add your miners, or check that your machines came over. Autotune, webhooks and node monitoring start **off**.

**Installer zip** (only if your browser refused the `.exe`): unzip `MinerHearth-Setup-v1.17.0.zip`, then run the `.exe` inside.

**Portable**
1. Unzip `MinerHearth-v1.17.0-portable.zip` next to your old folder (or anywhere you can write to, e.g. `Documents`).
2. Open the `miner-hearth` folder and double-click `MinerHearth.exe` (SmartScreen: **More info → Run anyway**).

**Uninstalling:** Windows Settings → Apps → Miner Hearth → Uninstall. Your data is kept unless you tick "Also delete my settings, machines and logs".

## Install (Umbrel, 5tratumOS, Linux)

See `README.md` inside `miner-hearth-umbrel-v1.17.0.zip` or `miner-hearth-5tratumos-v1.17.0.zip`, or `deploy/README-LINUX.md` in the repository. Not yet tested on real Umbrel or 5tratumOS hardware.

## Verify your download

In PowerShell, in the folder you downloaded to:

```powershell
Get-FileHash .\MinerHearth-Setup-v1.17.0.exe -Algorithm SHA256
```

The value must match the line in `SHA256SUMS.txt`:

```
c9afbc6e30acea1e7ff48744424a31f968a5b6e87ac1ba75ab087e94443151d7  MinerHearth-Setup-v1.17.0.exe
c33532a4c376036d5bf62b982bd218654eaafefd2183df3dec19dc2b628c1c08  MinerHearth-Setup-v1.17.0.zip
974df515be78cdd5d54906fe9253ac96e3eee4a291a930d461935e08b45ac2ec  MinerHearth-v1.17.0-portable.zip
822dfae74b978f371d487a3309e48772e98db6503bce26cfa520e4ff6d4a944b  miner-hearth-umbrel-v1.17.0.zip
d5e072d948994c3b7d702701c5c94913bed71936437ffa4b832a7d9f340900cb  miner-hearth-5tratumos-v1.17.0.zip
```
