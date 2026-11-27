# Miner Hearth on Umbrel (v1.17.1)

This folder is an **Umbrel Community App Store**: `store/umbrel-app-store.yml` (store id `minerhearth`) plus
one app, `store/minerhearth-miner-hearth/` (`umbrel-app.yml`, `docker-compose.yml`, `icon.png`, `data/`).
It runs the **headless web edition** of Miner Hearth in a container behind Umbrel's login-protected
`app_proxy`, with one persistent folder (`${APP_DATA_DIR}/data`, mounted at `/data`).

Published at:
- main repository: https://github.com/Miner-Hearth/Miner-Hearth-ASIC-Dashboard
- image: `ghcr.io/miner-hearth/miner-hearth:1.17.1` (amd64 + arm64)
- store repository: https://github.com/Miner-Hearth/Miner-Hearth-ASIC-Dashboard-Umbrel-Store

## 1. Publish the image (once per version)

Umbrel pulls a container image; it does not build one. `.github/workflows/docker.yml` in the main repository
builds **linux/amd64 and linux/arm64** and pushes `ghcr.io/miner-hearth/miner-hearth:<VERSION>` and `:latest`:

1. On GitHub: **Actions → docker → Run workflow** (or push a tag such as `v1.17.1`).
2. When it finishes, the run's summary shows the tag and its digest.
3. Organisation **Miner-Hearth → Packages → miner-hearth → Package settings → Change visibility → Public**
   (Umbrel cannot log in to a private registry).

Build it yourself instead (needs Docker with buildx):

    docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/miner-hearth/miner-hearth:1.17.1 --push .

## 2. The store repository

Upload the **contents of `store/`** (not the folder itself) to the root of the store repository's `main` branch,
so `umbrel-app-store.yml` sits at the top. The image is referenced by tag (`:1.17.1`), like Umbrel's community
store template. To pin it, add the digest from step 1: `ghcr.io/miner-hearth/miner-hearth:1.17.1@sha256:<digest>`.
Every app id must start with the store id: `minerhearth` → `minerhearth-miner-hearth`, and the folder name,
`id:` in `umbrel-app.yml` and `APP_HOST: minerhearth-miner-hearth_web_1` all use it.

## 3. Install on Umbrel

1. Umbrel → **App Store** → the **⋯** menu (top right) → **Community App Stores**.
2. Paste `https://github.com/Miner-Hearth/Miner-Hearth-ASIC-Dashboard-Umbrel-Store` and click **Add**.
3. Open the **Miner Hearth** store, click **Miner Hearth** → **Install**, then **Open**.

### Manual install with docker compose (no store)

On the Umbrel (or any Linux machine) over SSH:

    mkdir -p ~/miner-hearth/data && sudo chown 1000:1000 ~/miner-hearth/data
    cp deploy/docker-compose.yml ~/miner-hearth/ && cd ~/miner-hearth   # from this zip
    # edit the image line and the trusted networks; to build the image locally instead of pulling it,
    # first run (in the source folder): docker build -t ghcr.io/miner-hearth/miner-hearth:1.17.1 .
    docker compose up -d

That compose file uses host networking and publishes port **8080**, outside Umbrel's login, so keep
`MINER_HEARTH_TRUSTED_NETWORKS` narrow (see deploy/README-LINUX.md).

## How it behaves on Umbrel

- **Access:** the container publishes no port. Umbrel's proxy (port 21880 on the Umbrel) asks for the
  Umbrel password, then forwards to the app; `MINER_HEARTH_TRUSTED_NETWORKS` tells Miner Hearth to give
  those forwarded requests full control. Other Umbrel apps on the same Docker network could also reach it.
- **Your Bitcoin node:** if Umbrel passes the Bitcoin app's `APP_BITCOIN_NODE_IP` / `APP_BITCOIN_RPC_PORT` /
  `APP_BITCOIN_RPC_USER` / `APP_BITCOIN_RPC_PASS` (and Electrs' `APP_ELECTRS_NODE_IP` / `APP_ELECTRS_NODE_PORT`)
  to the app, Settings → Node Monitoring shows them as "Umbrel" nodes when **Monitor nodes** and
  **Use the Umbrel Bitcoin / Electrs apps** are on. The credentials stay in the container's memory; they are
  not saved and never reach the browser. If the variables are empty on your Umbrel (Umbrel may only pass
  them to apps that declare the dependency), either add `bitcoin` to `dependencies:` in `umbrel-app.yml`
  (Umbrel will then require the Bitcoin app) or add the node by hand: the host and RPC port (usually `10.21.21.8` and `8332`),
  user and password shown in the Bitcoin app's **Connect** dialog.
- **Finding miners:** the container is not on your home network's broadcast domain, so add miners by IP
  address or set the subnet to scan (e.g. `192.168.1.0/24`) in Settings.
- **Host stats:** CPU, memory, disk and temperature are read from inside the container (`/proc`, `/sys`);
  the disk is the app's data folder unless you set another path.
- **Quick restart (live updates):** the app exits with code 75 and `restart: on-failure` starts it again.
- **Data:** `~/umbrel/app-data/minerhearth-miner-hearth/data` on the Umbrel (config.json, userdata/, logs/).
  Uninstalling the app in Umbrel deletes it.

## Not tested

This package was checked against Umbrel's community-store template and the compose files parse, and the
headless server was run on Linux with the same environment variables, but it has **not been installed on
real Umbrel hardware**, and the arm64 image (Raspberry Pi based Umbrels) is built by QEMU in CI only.
