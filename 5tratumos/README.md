# Miner Hearth on 5tratumOS (v1.17.0)

## What 5tratumOS is (research, 26 Nov 2026)

5tratumOS (by WillItMod) is a home-server OS / management layer for home mining: pools, nodes, miner
dashboards and an app store. It installs on **Debian 12/13, Ubuntu 22.04/24.04, Raspberry Pi OS (Pi 4/5)
or a Proxmox LXC** with an install script, or from its own ISO. It runs its services with **Docker**
(and systemd units under `/opt/5tratumos`). Apps come from its **Community App Store**: the first-party
**MAIN** and **DEV** stores, the third-party **GLOBAL** catalogue, and user-added **Custom Stores**.

How the stores are built, from their public repositories:

- **MAIN / DEV** (`WillItMod/5tratStore-main`, `-dev`) are laid out exactly like an Umbrel community app
  store: `umbrel-app-store.yml` at the top, one folder per app with `umbrel-app.yml` + `docker-compose.yml`
  using `app_proxy` (`APP_HOST` / `APP_PORT`) and `${APP_DATA_DIR}`. Their docs describe keeping "one
  recipe for both Umbrel generations".
- **GLOBAL** (`WillItMod/5tratStore-global`) uses its own documented recipe: per app folder
  `5tratstore-app.yml`, `docker-compose.yml`, `5tratstore-review.yml`, `LICENSES.md` and a 512×512
  `icon.png`; images pinned by tag **and** multi-arch digest; non-root, `no-new-privileges`; recipe-only
  (the image must come from the project's own registry); submissions stay `status: proposed` until the
  maintainers test and approve them; the validator also rejects any mention of the word "umbrel".
- **Custom Stores**: 5tratumOS lets you add your own store repository, but I could not find public
  documentation of the exact format it accepts or the exact menu path. Since its own MAIN / DEV stores
  are Umbrel-format repositories, the Umbrel-format store in `custom-store/` is the closest match, but
  that it loads as a Custom Store is **an inference, not verified**.

Nothing here invents a format: `custom-store/` follows the Umbrel community store template that
5tratumOS's own stores use, and `global-submission/` follows the GLOBAL `templates/app/` files.

## Three ways to install

First publish the image (`ghcr.io/<github-user>/miner-hearth:1.17.0`, amd64 + arm64) with the main
repository's **docker** GitHub Actions workflow and make the package public, as described in
`README-UMBREL.md` step 1 (in this zip). Then run `sh prepare.sh <github-user> <store-repo> <repo> sha256:<digest>`
in this folder to fill in the placeholders.

### A. Custom Store (expected to work; unverified)

1. Push the **contents** of `custom-store/` to a public GitHub repository, e.g.
   `<github-user>/miner-hearth-store` (`umbrel-app-store.yml` at the top).
2. In 5tratumOS open the App Store, add that repository URL as a **Custom Store**, then install
   **Miner Hearth** from it and open it.
3. If 5tratumOS refuses the store, use B or C and tell us what it said.

### B. GLOBAL catalogue (needs the maintainers' review)

`global-submission/miner-hearth/` is a ready GLOBAL submission. After `prepare.sh` (the digest must be
real; placeholder recipes are rejected), fork `WillItMod/5tratStore-global`, add the folder on a branch
`app/miner-hearth`, and open a pull request. Leave `5tratstore-review.yml` at `status: proposed`. The
maintainers allocate or confirm the port (33180 is only a proposal), run install / start / update /
uninstall tests and approve it; their validator (`python3 scripts/validate_store.py`) reports the
review and lifecycle fields as failing until then, which is expected (checked: no other errors).

### C. Plain Docker Compose on the 5tratumOS host (works without any store)

5tratumOS already has Docker. Over SSH:

    mkdir -p ~/miner-hearth/data && sudo chown 1000:1000 ~/miner-hearth/data
    cp manual/docker-compose.yml ~/miner-hearth/ && cd ~/miner-hearth
    nano docker-compose.yml        # image line, trusted networks
    docker compose up -d

Then open `http://<5tratumos-box>:8080`. This uses host networking (so miners can be found by scanning) and
port 8080; change `MINER_HEARTH_PORT` if 5tratumOS or another app already uses 8080. It is not behind
5tratumOS's login, so keep `MINER_HEARTH_TRUSTED_NETWORKS` to your own computer(s) (see `README-LINUX.md`).
To build the image on the box instead of pulling it, run `docker build -t ghcr.io/<github-user>/miner-hearth:1.17.0 image-source`
in this folder first (Docker then uses the local image; the `image-source/` folder is in this zip).

## Notes

- Node monitoring: add your 5tratumOS nodes (e.g. its BTC / XEC node apps) in Settings → Node Monitoring
  with their RPC address, port and credentials; 5tratumOS-specific auto-detection is not implemented.
- Not tested on a real 5tratumOS install.

## Sources

- https://5tratum.com/ , https://5tratum.com/install , https://5tratum.com/apps
- https://github.com/WillItMod/5tratum (install-linux.sh, docs/install/, CHANGELOG.md: "custom store hint",
  "UI port declared by an app store manifest")
- https://github.com/WillItMod/5tratStore-main (umbrel-app-store.yml, willitmod-axesim/ as an example app)
- https://github.com/WillItMod/5tratStore-dev/blob/main/UMBREL-COMPATIBILITY.md
- https://github.com/WillItMod/5tratStore-global (README.md, CONTRIBUTING.md, STORE_POLICY.md,
  templates/app/, uptime-kuma/, scripts/validate_store.py)
- https://github.com/getumbrel/umbrel-community-app-store (the community store template)
