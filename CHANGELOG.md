# Changelog

All notable changes, newest first. Miner Hearth was called **Nonce Keeper** up to v1.16.3 (and Miner Autotune before v1.8); older entries keep the name the program had at the time.

## v1.17.1 (2026-11-27)
- **Dashboard background:** a firelit stone wall behind the dashboard (`assets/dashboard-bg.jpg`, 1280×720, 61 KB), fixed and scaled to cover the window, under a dark overlay (a light one in the Light theme) with slightly see-through panels so every number stays readable. Never used in the High contrast theme.
- **Settings → Appearance → "Dashboard background"** (on by default, saved with your other settings as `ui.background`). Turn it off for the plain background.
- **Footer** at the bottom of the dashboard shows the running version ("Miner Hearth v1.17.1").
- **Fix:** after a Quick restart, Settings → Live Updates kept saying "A quick restart is needed" and kept showing the Quick restart button. The saved flag is now cleared when the program starts and just before a Quick restart.
- **Shipped as a live update for v1.17.0:** the background is a dashboard stylesheet (`ui/mh-background.css`, image embedded) that loads at once; the setting and footer are program files and need Quick restart. Roll back returns to v1.17.0 (no footer). The program now knows its own version (`PROGRAM_VERSION`), so a live update reports the right version while the installed build's version is still used for "Roll back goes to …".

## v1.17.0 (2026-11-26)
- **Renamed: Nonce Keeper is now Miner Hearth.** In British English "nonce" is slang for a sex offender, so the old name read very badly to UK users. The program, window, tray, notifications, dashboard, installer, shortcuts, install folder (`%LOCALAPPDATA%\Programs\Miner Hearth`), Apps entry, sign-in entry, update packages, backups, saved-pool exports and docs all use the new name: `MinerHearth.exe`, `MinerHearth-Setup-vX.exe`, `userdata\miner_hearth.json`, update format `miner-hearth-update`. The word "nonce" now appears only where it means the mining nonce. Internal module names keep their `nk_` prefix.
- **Your data moves with you** (new `nk_legacy.py`): on the first start, if the Miner Hearth folder has no data yet, `userdata\`, `logs\` and a customised `config.json` are **copied** from the old install (installed or portable). Nothing is overwritten, the old folder is never changed, `userdata\updates\` is skipped, and the result is recorded in `userdata\migration.json`. Old backups, saved-pool exports, update packages and the old LAN request header are still accepted; old sign-in entries are moved.
- **Installer:** new component *Move from the old name (Nonce Keeper)*, ticked when an old install is found: closes the old program (asks first), removes the old program files, shortcuts, sign-in entry and Apps entry, keeps the old data for the copy above.
- **Live updates:** an old v1.16.x program can't live-update to v1.17.0 (the program itself was renamed): install v1.17.0 once with the installer or zip. Program files in an installed live update that are older than the running program are now ignored, so leftovers can never replace newer modules.
- **Node monitoring** (new `nk_nodes.py`, `nk_ui117.py`; Settings → Node Monitoring; off by default): Bitcoin Core / Knots RPC (sync %, height vs. headers / network, peers, mempool, size on disk; down / falling-behind / no-peers alerts with recoveries), other coins' nodes with the same RPC (eCash, CHTA, ...), LND (REST + macaroon) and Core Lightning (clnrest + rune): synced to chain and channels; Electrum servers (Fulcrum / Electrs): tip vs. your node or the network; host CPU, temperature, memory and low-disk alerts. Credentials stay in `userdata\nodes.json` (owner-only permissions) and never reach a browser; nodes can only be added or changed from the Miner Hearth computer. New webhook type "Node alerts". A Nodes panel on the dashboard.
- **Headless Linux edition:** `miner_dashboard.py --headless` (or `MINER_HEARTH_HEADLESS=1`; automatic on Linux without a display) serves only the web dashboard and runs until SIGTERM. `--host`, `MINER_HEARTH_PORT`, `MINER_HEARTH_HOME`, and `--trusted-networks` / `MINER_HEARTH_TRUSTED_NETWORKS` (browsers on those networks get full control, e.g. behind Umbrel's login proxy; everyone else keeps the LAN read-only / PIN rules). `MINER_HEARTH_RESTART_BY_EXIT=1` makes Quick restart exit with code 75 for a service manager or container to restart.
- **Docker:** `Dockerfile` (python:3.12-slim pinned by digest, non-root user 1000, data volume `/data`, health check), `.dockerignore`, `deploy/docker-compose.yml`, `deploy/miner-hearth.service` (systemd) and a GitHub Actions workflow that builds linux/amd64 + linux/arm64 and pushes `ghcr.io/<owner>/miner-hearth`.
- **Umbrel:** a community app store (`umbrel/store/`, app `minerhearth-miner-hearth`) using Umbrel's `app_proxy` and a persistent data folder; picks up Umbrel's Bitcoin node and Electrs when Umbrel provides them.
- **5tratumOS:** an Umbrel-format store for 5tratumOS Custom Stores, a GLOBAL-catalogue submission folder in 5tratStore format, and plain docker-compose instructions (`5tratumos/README.md`, with sources).

## v1.16.3 (2026-11-26)
- **Fix: a blank console window flashed every 2–3 seconds while Nonce Keeper was open** on PCs without Tailscale. The dashboard's 3-second status refresh asks for the PC's Tailscale address; when Windows didn't list one, Nonce Keeper ran `ipconfig` to look for it, and because NonceKeeper.exe has no console, Windows opened a new (empty) console window for `ipconfig` each time. `ipconfig` now runs hidden and at most once every 2 minutes (the answer is cached).
- **Every child process now starts hidden** through one helper (new `nk_proc.py`: `CREATE_NO_WINDOW` plus a hidden `STARTUPINFO` on Windows): the `ipconfig` fallback, the Windows notification (PowerShell), and Quick restart (detached, keeps its own window). A test fails if any module starts a process without it.
- The dashboard still opens exactly one window; alerts, sounds and notifications never create extra windows.

## v1.16.2 (2026-11-25)
- **Saved pools** (new `nk_pools.py`, `nk_ui1162.py`): a pool library in `userdata\saved_pools.json` (survives updates and restarts; ships empty). Entries: name, stratum address (`stratum+tcp://`, `stratum+ssl://`, or `host:port`, validated), username (`{worker}` = the machine's name when applied), password (default `x`, stored locally, never sent to the dashboard or LAN clients, shown masked), optional coin (auto-detected from the host). Add, edit (blank password keeps the saved one), delete, reorder; duplicates (same address + username) are refused. Up to 200 pools.
- **Save this pool** on each machine's pool info for the primary and fallback pool, and on each row of an Antminer / Braiins pool list; URL and username come from what the miner's adapter reports (the password isn't readable from miners, so it is saved as `x`; edit it if your pool needs one).
- **Pool editor picker**: choose a saved pool for the primary and the fallback (AxeOS / hexOS / NerdQAxe); fields are filled in, the saved password is used only while the address still matches the saved pool, and "Use this machine's name as the worker" swaps the worker part of the username. Manual entry works as before. Changes go through the same pool-change path, so the v1.15.5 pool-switch grace still opens.
- **Export / Import JSON** (Settings → Saved Pools); passwords are only exported on this PC and only when "include passwords" is ticked. LAN read-only mode blocks every change.

## v1.16.1 (2026-11-25)
- **Block alerts show the difficulty of the share that found the block**, no longer the machine's best / session-best difficulty. Sources, in this order: (a) the pool's block-list entry (HeliosPool `diff`); (b) the miner's last-share difficulty (AxeOS / hexOS / NerdQAxe `lastSubmittedDiff`) from the poll where the block trigger fired, only when that share is new and meets the network difficulty; (c) the block hash from the coin's explorer (the winning share hashes to the block hash, so its difficulty is the diff-1 target ÷ hash; SHA-256d coins). If none is available it says "unknown"; best difficulties are never used. CGMiner / Antminer / Braiins "Last Share Difficulty" is the pool's share target, not the share's own difficulty, so it is never used.
- Block Found and Block Confirmed show the same value for the same block (a value is fixed once shown; an "unknown" can be filled in by Block Confirmed from the block hash). The hourly summary lists each block's share difficulty.
- The pool block feed is read before the blockchain check, so a block the pool lists carries the pool's height and share difficulty in its first alert.
- Block candidates saved by v1.16.0 (whose stored value was a best difficulty) show "unknown" or the block-hash value instead.

## v1.16.0 (2026-11-25)
- **Live updates** (new `nk_update.py`, `nk_ui116.py`, `tools/make_update.py`), opt-in and off by default: apply an update package from a file or a URL you enter. Every file is checked against the package's SHA-256 manifest; optional Ed25519 signatures (pure-Python, checked against keys you trust), optional "require signature" and expected package SHA-256, app version limits, zip-safety checks (no path tricks, size/file limits, allowed paths only, Python files must compile, coin data validated). Coin data and adapter plugins hot-reload through `userdata\updates\`; dashboard CSS/JS reload in the page. **Quick restart** for program-file updates saves tuner/autotune state, pauses safely and resumes. **Roll back** to the previous set, **Remove all** to the shipped version, crash guard after two failed starts. Local-only management; no update server.
- **Simpler block alerts** on Discord, Telegram and ntfy: **Block Found #height** (Machine, Coin, Payout address from the pool username, Worker, Share difficulty, Network difficulty) and **Block Confirmed #height** with the same fields, sent only when the coin's own blockchain / explorer confirms. HeliosPool's "confirmed" list is a hint only and never called Confirmed. The "possible block" wording is gone. Difficulties are shown like the best share (2,345,678 → 2.35M; K/M/G/T/P). The block number is left off when unknown, never guessed; on HeliosPool the first alert waits up to 150 s for the pool's block height. Dedupe, hourly batching ("N more Blocks Found in the last hour") and the test button (new format) are kept.
- Adapter plugins (`plugins/*.py` in an update) are probed after the built-in specific adapters and before the generic one.

## v1.15.5 (2026-11-25)
- **Pool switches and restarts no longer raise fault alerts** (new `nk_grace.py`, on by default):
  - Detects a change of the *configured* pools for every family (AxeOS / NerdQAxe primary + fallback URL / port / user; CGMiner-style pool lists for Braiins OS+, Antminer, LuxOS, VNish, Avalon, Whatsminer and the rest), including changes made in the miner's own web page. Which pool is *active* is not part of this, so a real failover still raises the fallback-pool alert.
  - Nonce Keeper's own pool changes, fault-card restarts, scheduled maintenance restarts and restart-after-tuning-change open the same window before the command is sent.
  - Reboots are detected from uptime (also across Nonce Keeper restarts); a reboot within an hour of a pool change counts as part of it.
  - During the window (default 10 min, 1 to 60): no offline / hashrate-drop / no-shares / fallback-pool / pool-reject alerts. Start-up readings (sensor <= 0 °C, pool not connected, 0 or low hashrate, dead / missing chips or chains while initialising, fallback stratum, Wi-Fi not connected, the Braiins "starting" states) are shown as information instead of faults, so they do not alert, do not reach webhooks and do not hold tuning. These start-up readings are also ignored for the first 3 min after *any* reboot (configurable).
  - The window ends early after two healthy polls: online, hashing at least half the expected rate, not on the fallback pool, and a share accepted since the restart. Then the hashrate-drop and no-share baselines restart from the new pool.
  - If it has not recovered when the window ends, one alert of the usual kind says so with context, e.g. *"did not recover after the pool switch: still offline (timed out)"*. Your existing per-kind alert and webhook switches apply.
  - Overheat, VR temperature, power and hardware faults that are not start-up readings always alert, even during the window or a manual pause.
  - Card: blue info note ("Pool switch detected, restarting; restart alerts paused up to N min"), and **Pause alerts / Resume alerts** per machine for planned work (up to 12 h; survives restarts).
  - Event log: `pool_change_detected` (what changed, never the pool user), `maintenance_grace_start` / `_end` (recovered / expired), `maintenance_reboot_seen`, `maintenance_pause_start` / `_end`, and `alert_suppressed` for every alert or fault that was held back.
  - Settings → Pool Switches & Restarts: on/off, window length, start-up settle on/off and seconds.
- **Fix: block-found notifications never reached Discord / Telegram / ntfy** (error alerts did). Causes and fixes:
  - **Only a rising best share counted as a block.** After a machine's session or all-time best share is above network difficulty (after its first block, or on a long Braiins session), the best share never rises again, so later blocks were never seen. Nonce Keeper now also reads the firmware's own found-block counter (AxeOS `blockFound`, NerdQAxe `foundBlocks`, Braiins / CGMiner `Found Blocks`) as a trigger.
  - **A manual coin setting that does not match the pool hid every block.** Example: a machine set to BTC but mining on an `xec.` pool. Its shares were compared with Bitcoin's difficulty, which is 20,000× higher. Nonce Keeper now also watches the chain named by the pool host (and the miner's own job difficulty), and logs `coin_override_mismatch`.
  - **Nothing was sent until the explorer confirmed the block.** If confirmation failed for any reason, there was no notification at all. A **"Possible XEC block found by …"** message now goes to every webhook as soon as the share or counter is seen, followed by one **BLOCK FOUND** message (with an explorer link) once the coinbase is confirmed.
  - **Blocks found while Nonce Keeper was closed or the PC slept** were searched for only from the current chain tip forward, so they were rejected silently. The search now goes back over the whole gap.
  - **A manual payout address replaced the pool-username address.** Solo pools pay the pool-username address, so the block was rejected. Both are now checked.
  - Dedupe: one *possible* and one *confirmed* message per block, kept across restarts.
  - Every webhook delivery is written to the event log (`webhook_result`: service, status code, error, attempts; URLs and tokens are never logged).
  - A 429 rate limit (or a network or 5xx error) gets one retry.
  - Block messages are never paused by the pool-switch / maintenance grace.
  - New **Send test block notification** button next to *Send test* under Webhooks.
- **New: CheetahCoin (CHTA) and other low-difficulty coins**
  - CHTA is detected from the pool host (`chta.` / `cheetah`) or a `C…` address (Base58, version 0x1C); network difficulty and tip come from the CHTA Iquidus explorer (`chtaexplorer.mooo.com:3002`, plain HTTP). CHTA's difficulty changes wildly from block to block, so the miner's own job difficulty is used for the share check, and chain matching ignores difficulty for CHTA.
  - More block triggers: the AxeOS / hexOS **showNewBlock** banner flag (its `blockFound` is only 0/1 per boot), and a new last-submitted share at or above the job's network difficulty.
  - On fast chains every new winning share or counter step is its own block (no 30-min dedupe there). The same block is never counted twice: explorer confirmations can't claim a block another candidate already has, and a miner trigger plus a pool report of the same block merge into one.
  - **Pool block feed (HeliosPool, any of its coins):** for machines on `*.heliospool.com`, Nonce Keeper reads the pool's public block lists (pending, the last 128 KB of confirmed, orphaned) every 90 s and matches the machine's own pool address and worker locally (nothing is sent to the pool). Pool *pending* gives "Possible CHTA block found by …", pool *confirmed* gives **BLOCK FOUND** with an explorer link, *orphaned* gives a banner note. Blocks older than 2 h on first start are remembered silently. Settings → Block → "Ask the pool which blocks my machines found".
  - **Hourly limit:** at most N blocks per machine and coin are announced one by one per hour (default 6, Settings → Block). Further blocks are summed up in one "N more CHTA blocks found by … in the last hour" message.
  - **Coins Nonce Keeper doesn't know:** when the found-block counter goes up (or a share meets the job's own difficulty) on an unrecognised coin, a **"Block found (unconfirmed, coin unknown) by …"** message goes out, naming the pool host. It is no longer checked against the Bitcoin explorer unless the share was Bitcoin-sized. Settings → Block → can be turned off.
  - Coins with no reachable explorer still get the "possible" message, marked *unverified*.
- **Fix: AxeOS faults stopped updating** when the miner reported ASIC error counts (an internal error in the fault check). The last fault list then stayed on the card, and the tuner stayed in "fault hold". A fault seen once at boot could stay frozen for hours, such as a -1 °C sensor reading or a fan reading at start-up.
- **Fix: no frequency step-down for normal VR warm-up after a reboot.** The VR rapid-rise guard is skipped in the first 5 min of uptime while the VR is still 5 °C or more below its target. The target / max / emergency limits still apply.

## v1.15.4 (2026-11-25)
- **Full event & telemetry log** (new `nk_eventlog.py`, on by default because it stays on this PC and is redacted before it is written):
  - Daily JSON Lines files in `userdata\logs\` (`nk-events-YYYY-MM-DD.jsonl`; earlier days compressed to `.jsonl.gz`). Each line has a local ISO timestamp (`ts`), epoch time (`t`), `type`, `level` and `device`.
  - **Survives restarts and alert clearing.** Dismissing a banner or using Clear/Reset never deletes anything; the dismiss / clear action is logged too (`user_alert_dismissed`, `user_clear_fault`).
  - **Connection diagnostics:** `comm_lost` (exact error text, e.g. `CGMiner … summary failed: timed out`, exception class, API latency), `comm_still_failing` (every 20 failed polls or when the error changes), `comm_restored` (failure count, outage length, and whether the miner **kept running** or **rebooted** during the outage, from its uptime), `multi_device_comm_loss` when several machines drop within 90 s (points at a shared switch / cable / router), `poll_slow`.
  - **Miner reboots:** `device_rebooted` when uptime goes backwards, also across Nonce Keeper restarts.
  - Alerts (`alert`, `alert_raised`, `alert_recovered` for offline, hashrate drop, fallback pool, no shares, pool rejects, chip wear, hardware faults), `fault_appeared` / `fault_cleared` with the firmware's text, `tuning` (every frequency / voltage / power-target / fan / restart / reset change with reason and source), `setting_observed_change` (changes made outside Nonce Keeper), `pool_switch`, `pool_failover` / `pool_failback`, `pool_status_change`, `session_best_reset`, `tou_window_enter` / `_exit`, `quiet_window_enter` / `_exit`, `firmware_check`, `settings_changed` (diff, secrets removed), `app_start` / `app_stop` / `unclean_shutdown_detected`, `heartbeat` and `nk_gap_detected` (PC sleep / freeze), `uncaught_exception` and every INFO+ app log line (`log`, repeats de-duplicated for 10 min, tracebacks included).
  - **Snapshots** per machine every 5 min (configurable): hashrate (now / 1m / 5m / 15m / 1h / 24h), power and power limit, chip / board / VR / ambient temps, fans, frequency and voltage, accepted / rejected / stale shares, HW errors, best and session best, active pool and every pool's status, uptime, API polls / failures / latency, Wi-Fi RSSI (AxeOS), CGMiner counters such as Get Failures / Remote Failures (Braiins OS+), tuner state, active faults and alerts. All raw API fields hourly (configurable, 0 = off).
  - **Privacy:** same redaction as the diagnostics export (pool users / wallets, Wi-Fi names, MAC addresses, tokens, passwords, PINs, e-mail; IP addresses masked to `a.b.c.x`) plus Windows account names in paths and any value seen under a personal key, scrubbed again on export. Optional *Replace machine names with Machine 1, 2, …* on export.
  - **Size management:** daily rotation, kept 14 days and at most 200 MB (oldest first); thread-safe; a logging failure never affects mining or tuning.
  - **Settings → Full Event Log:** on/off, snapshot interval, raw-field interval, days kept, size cap, and **Export full log** (one zip with `nk-events.jsonl`, `manifest.json`, `diagnostics.json`, `log_status.json`).
- Windows installer `NonceKeeper-Setup-v1.15.4.exe` (per-user, no admin) alongside the portable zip; see `INSTALL.txt`.

## v1.15.3 (2026-11-24)
- **Antminer / Braiins OS+ session best matches active pool (`POOLS[i]["Best Share"]`)**:
  - Uses the active enabled pool's (`Stratum Active: true`) `POOLS` `Best Share` as the primary `session_best` value instead of miner-wide `SUMMARY["Best Share"]` (which counts from BOSminer boot), so Nonce Keeper matches Braiins' per-pool counter and HashBoard while keeping `SUMMARY["Best Share"]` for `Best` / all-time tracking.
  - Preserves failback protection in `SessionBestTracker` by tracking pool keys seen during the current miner boot (`seen_pools`): switching to a brand-new pool key starts at baseline `0` so any existing active-pool `Best Share` shows immediately, while failing back to a pool already seen in the same boot uses the saved pre-failover baseline.
  - Ignores disabled CGMiner pools (`Status: "Disabled"` or `Enabled: false`) when selecting the active pool and reading `Best Share`.

## v1.15.2 (2026-11-24)
- **TOU `All Machines` filter & live match note**:
  - Normalized common `"all"` filter tokens (empty, `all`, `all machines`, `all devices`, `*`, case-insensitive) across Time-of-Use windows, Quiet Hours, and Scheduled Restarts, and shared one filter helper across Braiins and AxeOS paths.
  - Replaced the literal `"All Machines"` default input value with placeholder text and added a live `Applies to: N machines (...)` readout on each TOU window card.
- **Session best difficulty resets on pool switches**:
  - Antminer (Braiins OS+ / CGMiner) and AxeOS devices now reset **session best** share tracking whenever the active pool changes (URL, user/worker, or active pool index, including failover and failback), instead of continuing to show the miner's power-on `Best Share`.
  - Post-switch session best state (`session_best.json`) is persisted across Nonce Keeper restarts so restarting the app does not revert to the miner's pre-switch power-on `Best Share`; all-time best remains preserved across pool switches and reboots.
- **Braiins OS+ diagnostics & tuning polish**:
  - `write_enabled`, `last_action`, `last_reason`, `voltage_reason`, and `firmware_note` now accurately reflect active Braiins power-target management instead of `monitor-only` wording.
  - Fixed per-chain `PowerLimitWatt` parsing from Braiins `tunerstatus`.
  - Capped `bos_cool_polls` at `bos_cool_polls_required` during cooldown so poll streaks do not inflate while waiting for cooldown expiry.

## v1.15 update (2026-11-24)
- **Bitaxe Gamma / AxeOS Time-of-Use (TOU) enforcement fixes:**
  - Enforced AxeOS TOU frequency caps (`axe_max_freq_mhz`) even when global or per-machine autotune is **OFF** (the v1.15 default), logging actions with source `schedule`, respecting dry-run and safety bounds, and safely restoring the pre-peak frequency when the window ends (while leaving post-window ramp-up to the autotuner when autotune is ON).
  - Hardened `apply_schedule_cap()` with autotune **ON** so an active TOU frequency cap always enforces `<= cap` on the next poll (bypassing cooldown) regardless of whether `decide()` returned `voltage_up`, `voltage_down`, `hold`, `step_up`, `step_down`, `fault_hold` (powered), or `emergency_down` (clamping emergency step-down to `<= cap`), and even if `cfg.max_freq` already equaled the cap while the miner was running higher.
  - Added fallback matching against the miner's AxeOS `hostname` and IP in TOU device filters, numeric coercion for string window cap values, and a clear UI warning if an enabled TOU window leaves the Axe frequency cap blank.
- **Removed TOU temperature-cap field:**
  - Removed the per-window TOU `ASIC target` (`axe_asic_target_c`) field from the UI, settings, and tuner logic; normal global or per-machine temperature targets and emergency limits always continue to apply during TOU windows. Existing saved settings migrate cleanly (dropping the obsolete key).

## v1.15 (2026-11-24)
- **Time-of-Use (TOU) Schedule fix & live dashboard status:**
  - Saving TOU schedules now shows an inline `"Saved."` confirmation inside the TOU Settings box (`#nkScheduleNote`) plus a floating toast notification, and syncs unblurred DOM inputs (`HH:MM` and `12h AM/PM`) cleanly before posting.
  - Active TOU windows apply **immediately on save**: saving TOU or Quiet settings wakes the tuner loop right away, AxeOS frequency caps bypass step cooldown (`180s`) and jump straight to the capped MHz on the next poll, Braiins OS+ watt caps bypass `bos_cooldown_sec` when above target, and device-name filters match case-insensitively.
  - Dashboard header banner (`#nkTouBar`) and machine card badges now display live TOU state (`TOU: <window> · cap X MHz until HH:MM`, or `TOU: off-peak` with next scheduled window).
- **Autotune OFF by default & persistent state:**
  - Both global autotune (`Autotune: OFF`) and every newly added machine (LAN scan, manual add, Tier-2 custom add) default to **OFF** on a fresh install.
  - Global and per-machine autotune on/off states persist across closing and reopening Nonce Keeper (`userdata\devices.json` + `autotune-state.json`). Existing users upgrading keep their previously saved state.
- **Tailscale remote access:**
  - Added a **Remote & Tailscale Access** guide and an optional **Tailscale only (`100.x` tailnet IP)** bind mode alongside `0.0.0.0` in Settings → Phone / LAN / Tailscale Access (`tailscale serve` proxying to localhost remains the recommended default; a PIN is strongly recommended).
- **Header & UI polish:**
  - Renamed header label to **Donate BTC**, removed legacy `"formerly miner-autotune"` / `"formerly Miner Autotune"` subtitles from the UI and README heading, and cleaned up responsive card/settings alignment, wrapping badges, and header button spacing.

## v1.14 (2026-11-24)
- **Manual removal of machines:** Every machine card now has a **Remove** button (with a confirmation dialog) so disconnected, offline, or retired miners can be removed directly from the dashboard. Removing a machine deletes it from `config.json` / `userdata\devices.json`, cleans its saved overrides, state, history, fault buffers/records, alerts, and block-odds caches, and safely skips any in-flight poll without crashing or resurrecting stale cards.
- **Full paste support & webhook URL UX (`nk_ui114.py`, `nk_ui19.py`, `nk_extras.py`):**
  - `Ctrl+V`, `Shift+Insert`, and a right-click context menu (**Cut**, **Copy**, **Paste**, **Select All**) now work across every text and URL input in the dashboard (including Windows `pywebview`), backed by a localhost-only clipboard bridge (`GET /api/clipboard` + `window.pywebview.api.read_clipboard`).
  - Discord and ntfy webhook URL fields now render as unmasked plain text inputs so the full URL is visible locally.
  - Pasted URLs automatically trim surrounding whitespace and newlines; Discord webhook URLs validate `https://discord.com/api/webhooks/` or `https://discordapp.com/api/webhooks/` with a clear error message; and **Test** webhook alerts now report the exact HTTP status and Discord JSON error body (e.g. `401 Invalid Webhook Token`, `404 Unknown Webhook`) in the UI.
- **Tier 2 & Generic compatibility (`nk_adapters/`):**
  - **MicroBT Whatsminer (`nk_adapters/whatsminer.py`):** `btminer` v2 (`:4028`) and v3 (`:4433`) monitoring, plus opt-in SHA-256 salted v3 write commands (`power_mode`, `power_pct`, `suspend`/`resume`, `reboot`).
  - **Goldshell (`nk_adapters/goldshell.py`):** HTTP `:80 /user/login` JWT Bearer auth + `/mcb/status`, `/mcb/pools`, `/mcb/setting` monitoring across Scrypt/kHeavyHash/BLAKE3, plus opt-in workmode & restart.
  - **Elphapex (`nk_adapters/elphapex.py`):** HTTP Basic Auth `/cgi-bin/stats.cgi`, `/cgi-bin/pools.cgi`, `/cgi-bin/luci/getworkmode.cgi` monitoring (`DG-Home1`/`DG1`), plus opt-in workmode & reboot.
  - **IceRiver (`nk_adapters/iceriver.py`):** Stock HTTP `:80 POST /user/userpanel` (`post=4`) and IceRiver-OC (`:4028` + HTTPS `:443 /api/overview` Bearer) monitoring (`KS0`–`KS5L`, `AL0`), plus opt-in OC settings & reboot.
  - **Bitdeer Sealminer (`nk_adapters/sealminer.py`):** Fixed non-closing BDMiner `:4028` TCP reads (`json.JSONDecoder().raw_decode` incremental parsing without waiting for EOF/`\x00` timeout) for `A2`/`A3`/`A4` read-only monitoring.
  - **Generic fallbacks (`nk_adapters/generic.py`):** Read-only `generic-cgminer` (`:4028`), `generic-prometheus` (`/metrics` with custom metric-name mapping), and `generic-http-json` (custom URL + dotted-key mapping), plus manual **Add Tier-2 / Generic Miner** form and Tier-2 detection/redaction in Compatibility Capture.
  - *Note on Tier 2 verification:* All Tier 2 and generic adapters were tested against mock servers built from documented vendor/community API specifications; they have **not** been tested against physical Tier 2 hardware. All Tier 2 families default to **monitor-only** until **Allow write controls** is explicitly ticked per device.
- **Bitaxe Gamma 601 error-rate & voltage-floor fix (v1.14 update):**
  - Prevents rolling-window dilution from lowering voltage during an error spike: `decide()` now refuses `voltage_down` whenever the latest `errorPercentage` is `>= error_low_pct` (`0.5%`), resets the low-error streak, and triggers `voltage_up` (or `step_down` at the `1250 mV` cap) immediately on `max(rolling, latest) > error_high_pct` (`1.0%`), including `+20 mV` / `60 s` severe recovery at the `850 MHz` cap.
  - Adds a per-frequency learned stability floor (`bad_voltage_by_freq`, persisted in `userdata\tuner_safety.json` across restarts and cleared on per-device override reset) so once a voltage produces out-of-tolerance hardware errors at a given frequency, autotune never re-descends back into that unstable voltage.
  - Restricts `voltage_down` to verified hardware `errorPercentage` readings after warmup (`uptimeSeconds >= 90` when reported, `hashrate > 0`) with a full `error_window_polls` window, never on `sharesRejected` / `hashrateGap` fallbacks or missing (`None`) readings (which no longer return stale cached averages).
  - Dashboard cards now display the active error metric source (`errorPercentage` or fallback) under Error Rate and any active `learned floor <mV> mV` in the Voltage reason bar.

## v1.13 (2026-11-23)
- **Pluggable Tier-1 adapter layer (`nk_adapters/`):** Canonical capability flags (`AdapterCapabilities`) and unified hashrate scaling (`H/s`..`EH/s` and `Sol/s`..`GSol/s`) across all Tier-1 families while keeping existing AxeOS, NerdQAxe+/NerdOCTAxe, Stock Bitmain, and Braiins OS+ behavior regression-free.
- **Canaan Avalon (`nk_adapters/avalon.py`):** Monitors `Nano 3`/`3S`, `Avalon Q`, `Mini 3`, and `Avalon Home` over CGMiner `:4028` (`summary`/`stats`/`litestats` + `MM ID0[...]` key-value parser). Optional opt-in `ascset` controls (`workmode` Eco/Standard/Super, `soft_off`/`soft_on`, `reboot`) are **off by default** per device.
- **VNish & LuxOS (`nk_adapters/vnish.py`, `nk_adapters/luxos.py`):** VNish `:80 /api/v1/*` + `:4028` monitoring with opt-in `POST /api/v1/unlock` throttle (`20–100%` in 5% steps), preset, pause/resume/reboot; LuxOS `:4028`/`:8080` monitoring with opt-in `logon` -> `profileset` -> guaranteed `logoff` in `finally`. Both monitor-only by default.
- **Bitmain Stock Modern & Legacy (`nk_adapters/bitmain.py`):** Monitors `S21`, `T21`, `S19*`, `L7`, `L9`, `KS3`/`KS5`, `Z15`, and `E9` with automatic algorithm/unit inference (`sha256d`, `scrypt`, `kheavyhash`, `equihash` `Sol/s`, `etchash`) and **user-supplied HTTP Digest credentials only** (never auto-tries default passwords).
- **Home Miners:** Added **FutureBit Apollo** (`BTC`/`II`) `:4028` monitoring, **Bitaxe fork** ASIC whitelist (`BM1362`–`BM1398` for `hexOS`, `LV06`–`LV08`, `Bitaxe Touch`, `QAxe`), and **Braiins Mini Miner (`BMM 100/101`)** support with dynamic `GetConstraints` power floors (`>= 1 W`) and `:8081/metrics` Prometheus fallback.
- **Multi-algorithm coins, odds & explorers (`nk_coins.py`, `nk_math.py`):** Added `LTC+DOGE` (merged AuxPoW), `KAS`, `ALPH`, `CKB`, `ETC` (without `2^32`), `ZEC` (`Sol/s`), `DASH`, and `HNS`, with graceful `"unknown"` fallback.
- **Dual-side chart Y-axis range labels (`nk_ui113.py`):** Low/high range labels now render on **both left and right sides** of every chart (24h card sparklines and history panel charts), computed dynamically from visible data (updating on time-range, zoom sliders, and series toggles), with dual-unit left/right labels when Hashrate + Power (W) share a chart.
- **Compatibility capture (`nk_compat.py`):** One-click **Compatibility capture** button on every device card and in the Network Scan bar for any arbitrary IP/unknown miner. Collects read-only API probes into a `.zip` in `userdata/compat-captures/`, automatically redacts passwords, tokens, cookies, auth headers, all wallet address formats, pool usernames/workers, SSIDs, MAC addresses, serial numbers, public IPs, emails, and `.local` hostnames, runs a post-save scan over every zip entry, and displays a redaction summary modal with counts by type before download.

- **Bitaxe Gamma 601 voltage-tuning fix (v1.13 update):** Recognizes single-ASIC `boardVersion` `600`–`609` Bitaxe Gamma boards even with custom hostnames/missing `deviceModel` (instead of misclassifying as `axeos-generic` with voltage control disabled), auto-promotes previously saved misclassified Gamma 601s on the first poll, adds an **Auto voltage control** toggle in per-machine **Overrides** (`gamma` / `axeos-generic` only; NerdOCTAxe remains locked frequency-only), falls back to `coreVoltageActual` if `coreVoltage` is absent, and accelerates high-error (`>= 4%`) voltage recovery at the frequency cap (`+20 mV` step, `60s` settle cooldown, strictly clamped to the `1250 mV` hard cap).

## v1.12 (2026-11-23)
- **Pool-username payout address extraction:** When you don't set a manual payout address for a coin, Nonce Keeper extracts valid payout addresses (`bc1`/`1`/`3`, `ecash:`/`bitcoincash:` CashAddr, raw `[qp]...` CashAddr, legacy BCH/BSV) from each machine's stratum `pool_user` (stripping `.worker`), ignoring non-address pool account usernames with a note in Settings → Coins. Manual payout addresses still override, and diagnostics keep addresses redacted.
- **False fan-stall fix for Bitaxe Gamma 601s without a tachometer:** Suppresses `axeos:fan_stalled` when `fanrpm` has always been `0` since boot / model has no tach sensor, showing `"speed % only (no RPM sensor)"` on cards and in Details. If a machine previously reported nonzero RPM and then drops to `0`, `axeos:fan_stalled` still fires.
- **OCTAxe rail-off root-cause detection (`axeos:chip_power_rail_off`):** Detects when `coreVoltageActual == 0` (with `coreVoltage > 0`) or `power <= 1.0 W` and `currentA <= 0.1 A` while input voltage is present and mining isn't paused — even when firmware v1.0.37.1 clears `shutdown=false` and `boardError=0` after a watchdog reboot. Treats all-zero `asicTemps` + `0 W` as supporting evidence and attaches Nonce Keeper's last remembered VR overheat (`userdata/fault_context.json`) as the likely cause.
- **Automatic 12-hour pre-fault high-resolution captures:** Keeps a bounded rolling buffer (`userdata/fault_buffers/<device>.json.gz`, persisted across restarts) of every poll (~15 s, full telemetry + `raw_fields`). Whenever any fault appears, Nonce Keeper automatically saves the preceding 12 hours (configurable 1–72 h via `faults.record_hours`) to `userdata/fault_records/<device>_<timestamp>.json.gz`, deduping/extending if a fault re-fires within the window. Records stay until you delete them and can be viewed in-app, exported (redacted), or deleted from **Details → Diagnostics**.
- **In-app Diagnostics viewer:** Clicking **Diagnostics** on any fault box opens a readable **Details → Diagnostics** panel showing Safety/Guard state, Active Faults with component attribution & timestamped causes, Pre-Fault Snapshot vs Current Readings, Fault History, Tuning Log, and an optional **Download redacted JSON** button.
- **Timestamped cause + component attribution + pre-fault snapshot:** Every fault shows `cause_iso`, `cause_summary`, exact component (`vrTemp` vs `vrTempInt`, `asicTemps` slot(s), `hashrateMonitor` chip errors, Antminer `chain_acsN` 1-based `x`/`-` chip positions, `DEVS[id]`, `TEMPS`), and matching firmware log line when available. Pre-fault healthy snapshots (`userdata/fault_snapshots.json`) are saved automatically before faults or `0 GH/s`.
- **Autotuner safety hardening:**
  - **`fault_hold`:** Holds autotune (no step-up) while `0 GH/s`, `0 W` / `coreVoltageActual == 0`, `shutdown`/`boardError`/`overheat_mode`/`power_fault`/`miningPaused`, or any active `problem` fault.
  - **NerdOCTAxe 800 MHz default cap + `overclock_opt_in`:** Caps Nerd/OCTAxe `max_freq` at firmware normal max (**800 MHz**) unless you explicitly tick per-device **Allow >800 MHz overclock (up to 850 MHz)** (with VR thermal warning).
  - **Post-emergency lockout & rollback:** Persisted in `userdata/tuner_safety.json` — emergency thermal drops roll back to `last_stable_freq` and lock upward steps for 3 hours by default (`tuner.emergency_lockout_min` = `180`, configurable in Settings).
  - **Electrical, rate-of-rise & sensor plausibility guards:** VR rate-of-rise guard (`>= +4 °C` in `<= 65 s` above `52 °C`), power/current `>= 85 %` hold-up / `>= 95 %` step-down, input voltage sag (`< 11.6 V` / `< 11.3 V` on 12 V boards; `< 4.85 V` / `< 4.65 V` on 5 V Bitaxe), `30 s` settle on thermal `step_down` (bypasses `180 s` upward cooldown), and `tuner:sensor_suspect` guard (`> 25 °C` single-poll jump or `VR > 105 °C` while `ASIC < 45 °C`).
- **Migration:** Device schema `3` (`userdata/devices.json.v2.bak` created once) caps existing Nerd/OCTAxe saved `max_freq > 800` to `800 MHz` unless `overclock_opt_in` is set; settings schema `4` (`userdata/nonce_keeper.json.v3.bak`) adds `tuner.emergency_lockout_min = 180`.
- **Limits:** Pool-username payout extraction only works when the stratum username is an actual address (not an account name); raw CashAddr requires valid checksum. Gamma no-tach heuristic assumes a fan with no tach never reports nonzero RPM; if a fan actually stalls before ever being seen spinning on first boot, a sensor-less Gamma cannot distinguish 0 RPM from a missing tach. Pre-fault snapshots require at least one healthy poll before the fault.

## v1.11 (2026-11-23)
- **Full firmware-alert mirroring across AxeOS, NerdQAxe+/NerdOCTAxe, Stock Bitmain Antminer, and Braiins OS+:**
  - **AxeOS (ESP-Miner):** added detection for UI banners and inline alerts — high ASIC temp (`>= 70 °C`), high VR temp (`>= 105 °C` / warn `>= 94 °C`), stalled fan (`0 RPM` while `fanspeed >= 20 %`), temperature sensor error (`<= 0 °C` while active), input voltage sag even when `nominalVoltage` is absent, `poolConnectionInfo = "Not Connected"`, `isUsingFallbackStratum` + `primaryPoolError`, `poolDifficulty < asicDifficulty`, `coinbasePayoutStatus = "not_found"`, `frequency` below `min(frequencyOptions)`, disconnected/weak Wi-Fi (`wifiStatus` / `wifiRSSI <= -85`), 0 GH/s online, `< 50 %` of `expectedHashrate`, dead ASICs in `hashrateMonitor`, and `errorPercentage >= 5 %`.
  - **NerdQAxe+ / NerdOCTAxe:** immediate first-poll `/api/v2/dashboard` probing (even if added as `gamma` or `axeos-generic` whenever `deviceModel`/`version` matches NerdQAxe), preserving `0 RPM` entries in `thermal.fans`, stalled fan (`0 RPM` at `>= 35 %` speed) & low-duty fan hint, red Stratum tile flags (`connected = false`, `verifyBlocked`, `poolDiffErr`), input voltage outside `[minVoltage, maxVoltage]`, high `currentA` / `power`, VR & ASIC temp limits, and single dead `asicTemps` sensor (while keeping v1.0.37.1 all-zero `asicTemps` clean).
  - **Stock Bitmain Antminer (CGMiner + web UI):** fixed `antminer_fault_fields` handling of `miner_count` / `chain_num` / AML `chain[]` arrays and baseline chain-count drops across polls; added slow-fan detection (`< 35 %` of sibling median RPM), 0 GH/s online after boot warmup, `< 75 %` of nominal `rate_ideal`, high chip (`>= 85 °C`) / PCB (`>= 75 °C`) temp, temp sensor errors (`0` / negative / `>= 125 °C`), high `Device Hardware%` (`>= 2 %`), `DEVS[].Status` Dead/Sick, all `POOLS[]` Dead, CGMiner `STATUS = E/W/F` error messages, plus background probing of Bitmain `/cgi-bin/warning.cgi`, `/cgi-bin/summary.cgi` (`status[]`), and port `6060` `/warning` (when reachable / web password saved).
  - **Braiins OS+:** fixed `braiins_api._status_text` unwrapping nested protobuf-JSON `detailed_status` (`stopped:thermal_pause`, `dead_pools`, `missing_license`, `hardware_error`, `tuner_error`, `unsupported_hardware`, `application_unavailable`, `dps_cooldown`, `user_pause`, `starting:*`), added gRPC `MinerService/GetErrors` + `GetMinerDetails` fallback when REST is unavailable, and wired CGMiner-only (no `bos_password` needed) detection of `FANS[]` lost/slow, `TUNERSTATUS` Error/Disabled/Restricted, `DEVS` Dead/Sick, `TEMPS` sensor/overheat, and `POOLS` Dead.
  - **Repeated reboot loop:** detects `>= 3` unexpected uptime drops within 2 hours (`reset:loop`).
- **Tests:** added full fault-simulation matrix across all 4 firmware families (`V111FaultSimulationTests`).

## v1.10 (2026-11-23)
- **Fault flags + per-card error badge.** AxeOS (`overheat_mode`, `power_fault`, `hardware_fault`, `miningPaused`, `resetReason`; low input voltage when the firmware reports a nominal voltage), NerdQAxe+ (`boardError` 1–6, `shutdown`, `lastResetReason`, read from `/api/v2/dashboard` + `/api/v2/system` every 2 min), stock Antminer over CGMiner (`chain_acnN` = 0 chips, `chain_acsN` with `x`/`-` chips, fan at 0 RPM = fan lost), Braiins OS+ (error codes U1…H2 and paused / thermal-pause status via the saved login). Reset reasons (brownout, watchdog, panic) are shown as info for 6 h after a reboot.
- **Explainer catalog** (`nk_faults.py`, `nk_fault_codes.py`): 74 entries incl. all 37 Braiins OS error codes, each with meaning, likely cause, first checks and a guide link (bitaxe.de, OSMU wiki, D-Central, Braiins Academy, Bitmain support, GitHub). Only documented codes are mapped; everything else is shown verbatim as "Unmapped: …".
- **Fault history** timeline (dashboard section + Details → Faults): appeared / cleared (with duration) / user reset / auto-restart, in `userdata\fault_history.json` (last 2000). In-app alert when a fault appears (cooldown per fault); optional **Hardware fault** webhook type, off by default.
- **Manual Clear / Reset** per fault: AxeOS `PATCH /api/system {"overheat_mode":0}` or `POST /api/system/resume`, restart for a power/hardware fault; NerdQAxe+ restart (OTP-protected miners ask for the code); Braiins OS+ Resume after a pause, otherwise Restart — **never factory reset**; stock Bitmain `GET /cgi-bin/reboot.cgi` with HTTP Digest **only** when you saved the miner's web password. Always asks for confirmation; disabled while offline, while hot (clear/resume and overheat restarts wait until chip < 45 °C and VR < 65 °C; other actions are blocked at your max temps), at most one per 10 min and 4 per day per machine (configurable); dry-run only logs; every action is written to the fault history and the tuning timeline. **No auto-clear.**
- **Capture log**: manual snapshot of AxeOS `/api/system/logs`, Braiins OS+ `/api/v1/miner/log/errors` and stock Bitmain `/cgi-bin/log.cgi` (saved login), kept in `userdata\log_snapshots\` (256 KB cap) and scanned for known lines (ASIC not found, chip-ID mismatch, fan lost, find 0 asic, PIC/EEPROM errors, PSU errors, temperature protection…).
- **Diagnostics export**: one JSON per machine (or all) with settings, readings, faults, history and the tuning log, with pool users/wallets, payout addresses, Wi-Fi names, MAC addresses, tokens, passwords and host names removed and IP addresses masked (`192.168.1.x`).
- **Details button** on every card: Components tab (all temps, voltages/currents/power, every fan, chains/chips, frequency, firmware, uptime, reset reason, Wi-Fi RSSI) and an **All fields** tab listing everything else the API returns under its API name (grouped, with a filter; personal fields left out). Read-only; °C/°F and themes respected.
- **Migration:** settings schema 3 with a one-time `nonce_keeper.json.v2.bak`. 15 new tests (127 total) using mock fault scenarios (`python mock_server.py --faults` starts the mocks with example faults).
- **Limits:** Braiins OS+ errors/status use its REST API (`/api/v1/miner/errors`, `/details`) and need the saved login; the detailed status is best-effort and depends on the Braiins version. NerdQAxe+ firmware has no log endpoint, so *Capture log* is not offered for it. AxeOS `/api/system/logs` exists only on firmware that offers it. Stock Bitmain reboot/log need your web login (stock firmware cannot clear a fault any other way). Codes outside the catalog are shown as "Unmapped". The badge shows what the firmware reports; a hardware problem that the firmware does not flag cannot be detected. Fault Clear/Reset is never automatic.

## v1.9 (2026-11-22)
- **Per-coin block odds & confirmation:** each machine's SHA-256 coin (BTC, BCH, BSV, XEC, DGB-SHA256, Fractal Bitcoin) is detected from the miner's stratum job (block height / network difficulty vs. each chain's live tip), the payout-address format and pool host hints; "unknown" when unsure, with a per-machine override. A **Block odds per coin** panel uses each coin's own difficulty. Block confirmation checks that coin's explorer (mempool.space/blockstream for BTC, Haskoin for BCH, WhatsOnChain for BSV, Chronik for XEC, digiexplorer for DGB, mempool.fractalbitcoin.io for FB) against payout addresses set per coin; addresses are compared as output scripts (legacy, bech32/bech32m and cashaddr). Other chains are only queried when a machine may be mining them. Unknown-coin machines keep v1.8 behaviour (BTC).
- **Session best share** for Antminer / Braiins OS+, next to the all-time best, persisted in `userdata\session_best.json`.
- **Pool health:** rejected/stale shares, reject rate, ping and ping loss per machine; new **Pool rejecting shares** alert (default 5% over 30 min, at least 10 shares). **Pool comparison** panel: per-pool accepted/rejected, latency (TCP connect from this PC every 2 min) and uptime, 24 h and 7 days (`userdata\pool_stats.json`).
- **Ambient / intake temperature** where miners report it.
- **Chip health:** best stable frequency/voltage per day (`userdata\chip_health.json`) and an optional **chip wear** notice when the same frequency needs ≥ 20 mV more than its early baseline (≥ 14 days apart; both configurable).
- **Quiet mode:** scheduled fan cap windows for AxeOS machines with autotune on; the window can also cap frequency. Emergency temperatures release the fan to automatic at once and suspend quiet mode for 15 min; turning autotune off or closing the app also restores automatic fan. Changes go to the tuning log (source *quiet* / *emergency*).
- **Maintenance restarts:** weekly restart (day + time) and hung-miner restart (hashrate 0 while the API answers for N min, default 15). Max per 24 h (default 2), minimum gap (default 60 min), 15-min grace after a restart, never when offline; dry-run only logs. AxeOS via its restart API, Antminer via Braiins OS+ only. Off by default (`userdata\restarts.json`).
- **Fleet comparison** table (sortable) and a dashed **24 h average** line + label on the fleet hashrate chart. No daily recap.
- **Webhooks:** Discord, Telegram, ntfy; per-alert-type toggles, **Send test**, off by default. Secrets are hidden from LAN clients and excluded from backups unless "Include passwords" is ticked; restoring a backup without them keeps the current ones.
- **Themes** (dark, light, high contrast) and **°C/°F** display.
- **Layout:** reorder card fields, summary-bar items and dashboard sections by drag-and-drop or ▲/▼ in Settings → Layout; persisted in `userdata\nonce_keeper.json`, works with the existing visible-field toggles, *Reset layout to default*.
- **Migration:** settings schema 2 with a one-time `nonce_keeper.json.v1.bak`. 23 new tests (112 total).
- **Limits (scoped down):** coin detection uses the block height / network difficulty the miner reports (stock AxeOS / CGMiner APIs do not expose the raw stratum prevhash or coinbase); quiet mode is AxeOS-only (S19s keep using the schedule's power-target ceiling); automatic Antminer restarts need Braiins OS+; earnings estimates stay BTC-only (alt coins get odds, not earnings); pool latency is measured from this PC; BSV and XEC confirmation rely on one public explorer each.

## v1.8 (2026-11-22) — Nonce Keeper
- **Rebrand to Nonce Keeper:** header, window title, `NonceKeeper.exe`, version info, PyInstaller spec (`packaging\NonceKeeper.spec`), README. Header shows `Donate BTC: bc1q3m8usp6mffv4txhwcltq9lu9jltx02eyukgrvf` right of the name and version, with a **Copy** button (Clipboard API → pywebview/Win32 clipboard → `execCommand` fallback) and "Copied" feedback, in the app window and in a browser.
- **Upgrade safety:** existing `userdata\devices.json`, `history\`, `last_share.json` are untouched; new v1.8 settings live in `userdata\nonce_keeper.json` (created with defaults). Legacy autostart is moved to the new exe; same single-instance lock as v1.7.
- **Block-found notification that can't false-alarm:** trigger on share/best-session difficulty ≥ network difficulty (only on a new, higher value; first sighting after start is a baseline). Confirmation looks up blocks around the trigger time on mempool.space (fallback blockstream.info, or your own mempool/Esplora base URL) and requires a coinbase output paying a configured payout address. Gives up after 6 blocks ("rejected": stale share, pool payout, other miner) or 48 h without the explorer ("unverified"). Only a confirmed block gets the banner, sound and Windows toast.
- **Problem alerts:** offline (default 3 min), hashrate drop (default 30% vs 30-min baseline, sustained 5 min), fallback pool (AxeOS flag or S19 active pool priority), no shares (default 30 min). Toggles, thresholds and a shared cooldown (default 30 min) in Settings. Recovery shows a quiet "back online" banner.
- **Sounds:** seven original 8-bit sounds synthesized in code (`nk_sounds.py`, no audio files copied from anywhere), a picker per alert type with preview, master mute, volume, and "load your own .wav/.mp3" (stored in `userdata\sounds\`, max 5 MB).
- **Notifications:** banner in every open dashboard, sound in the window (or through the PC speaker when no window is open), Windows toast via built-in PowerShell (no extra dependencies), and "Test problem/block alert" buttons.
- **Power cost & profit:** rate per kWh + currency; cost/day per machine and fleet; expected sats/day from hashrate ÷ (difficulty × 2³²) × (subsidy + optional average fees of the last 144 blocks), labeled as an estimate; BTC price (mempool.space, fallback Coinbase); break-even electricity rate.
- **Time-of-use scheduling:** day/hour windows (overnight supported) with per-window Axe frequency cap and Braiins power-target ceiling, optional per-machine list. Implemented as tighter limits for the tuner, so it keeps tuning inside them and emergency handling always wins.
- **Block odds:** per machine and fleet, chance per day/month/year and expected time to block.
- **Longer history:** 7-day and 30-day downsampled storage (`userdata\history_long\`), charts with 24h/7d/30d switch per machine or fleet, CSV export.
- **Tuning log:** timeline panel of every change with reason and source, persisted to `userdata\tuning_log.jsonl` with rotation.
- **Phone access:** opt-in LAN listener on its own port (default 8081), optional PIN (cookie, lockout after 10 wrong tries), read-only mode (default on) that blocks all write endpoints for LAN clients; writes also need a custom header (blocks cross-site form posts). This PC's window is never locked. LAN URL shown in Settings. Mobile layout.
- **Settings backup/restore:** export/import one JSON file; Braiins passwords, OTP secrets and the LAN PIN only with "Include passwords" (never for LAN clients). Restoring a password-less backup keeps the existing secrets of same-named machines.
- **Firmware update notices:** daily, cached in `userdata\firmware_cache.json`; badge on the card links to the release.
- **Network stats** in the header: height, difficulty, next adjustment % and ETA (mempool.space endpoint, or estimated from the epoch's block times on Esplora).
- New display-field toggles for all of the above. 25 new tests (89 total).

## v1.7 (2026-11-22)
- **Versioning (`VERSION` = `1.7`):** Release zip `miner-autotune-v1.7.zip` (and refreshed `miner-autotune.zip`), updated `MinerDashboard.exe`.
- **Customizable Dashboard Card Order:** Reorder machine cards using `◀` / `▶` buttons on card headers, drag-and-drop, or the **Settings -> Machine Card Order** list; saved in `userdata/devices.json`.
- **Settings — Toggleable Display Fields:** Every displayable metric/panel (Fleet Summary, Hashrate, ASIC/VR temps, Fans, Frequency, Core Voltage, Power & Efficiency, HW Error Rate, Last Share, Shares & Best Diff, WiFi & System, ASIC Domains, Sparklines, Voltage Banner, Manual Control, Pool Editor, S19 Hashboards, S19 Power Target, Action Log, Recent Logs) can be toggled on/off individually in **Settings**, persisted in `userdata/devices.json`.
- **Settings — Global Caps & Targets + Per-Machine Overrides:**
  - Global tunable defaults: **850 MHz** cap, **1150–1250 mV** core voltage, **62.5 °C** ASIC emergency, **65.0 °C** VR emergency, **50 MHz** emergency frequency drop, **1210 mV** emergency voltage reset, **0%** error target with **0.5–1.0%** acceptable band.
  - Optional per-machine override panel on each tunable card (leave blank to inherit globals, or **Reset to Global**).
  - `config.json` remains shareable (`"devices": []`); upgrading from v1.3–v1.6 automatically preserves devices, history, and last-share state while migrating settings.
- **Updated Axe Autotune Logic (by device capability, never by IP/hostname):**
  - **Normal (voltage-tunable):** Steps frequency up while cool until error rate leaves tolerance (`> 1.0%`), raises core voltage (`+10 mV`, max `1250 mV`) until back in tolerance (`<= 1.0%`), and trims voltage down when `< 0.5%` to find the lowest stable voltage without chasing `0%` with high voltage.
  - **Emergency (`ASIC >= 62.5 °C` or `VR >= 65.0 °C`):** Drops frequency by `50 MHz` (bypassing cooldown) and sets voltage to `1210 mV` (leaving it unchanged if already below `1210 mV`) until normal tuning resumes.
  - **Frequency-only + VR-relief (OCTAxe):** Holds `1210 mV`, tunes with frequency only, and lowers voltage only when VR temperature is too high.
- **Optional Braiins OS+ S19 Power-Target Management (per-device toggle, default OFF):**
  - Pure-stdlib Braiins OS+ Public API client (`braiins_api.py`): gRPC (`braiins.bos.v1` over HTTP/2 `h2c` on TCP `:50051`) with automatic REST (`/api/v1/...` on `:80`) fallback — zero heavy `grpcio`/`protobuf` dependencies in `MinerDashboard.exe`.
  - Works *with* Braiins' own per-chip tuner by gently nudging `SetPowerTarget` down when chips run hot and up slowly with thermal headroom, bounded by user-settable min/max watts and step and rate-limited by cooldown (`300s` default).
  - Includes a read-only **Test connection** button (`AuthenticationService/Login` + `GetTunerState` + `GetConstraints` + `GetMinerConfiguration`) reporting transport, current target, power constraints, and whether power-target writes are supported.


## v1.6 (2026-11-22)
- **Single-file desktop app `MinerDashboard.exe`** (Windows 10/11 x64, built with PyInstaller `--onefile`, windowed, no console):
  - Starts the autotune loop in a background thread and the dashboard server on `127.0.0.1` (port `8080` from `config.json`; if it is busy, a free port is chosen automatically).
  - Shows the dashboard in a native app window (pywebview + Microsoft Edge WebView2) instead of a browser. If WebView2 (or .NET Framework 4.7.2+) is missing, a message box explains it and the dashboard opens in your default browser instead; clicking **OK** in that message stops the app.
  - Closing the window stops autotune and the server cleanly.
  - Only one copy runs at a time: launching it again brings the existing window to the front. `run.bat` also refuses to start while the app is running, so two copies never tune the same miners.
  - Logs go to `logs\autotune.log` (plus `logs\app-errors.log` for unexpected errors) because there is no console.
  - `config.json`, `userdata\` and `logs\` live **next to the .exe** (never in a temp folder). Dropping the exe into your existing v1.3–v1.5 folder picks up your saved machines, names, history and last-share tracking. If `config.json` is missing, the shipped default is created.
  - App icon and version info; `enable-autostart.bat` / `disable-autostart.bat` (or `MinerDashboard.exe --autostart on|off`) start the app at Windows sign-in.
  - Other options: `--dry-run`, `--port N`, `--browser` (force browser mode), `--config PATH`.
- **Source build files:** `build-exe.bat`, `packaging\MinerDashboard.spec`, `packaging\requirements-build.txt` (PyInstaller 6.11.1 + pywebview 5.3.2, build-time only) to rebuild the exe yourself.
- No changes to tuning rules, limits, or the dashboard data.

## v1.5 (2026-11-22)
- **Versioning (`VERSION` = `1.5`):** Release zip `miner-autotune-v1.5.zip` (and refreshed `miner-autotune.zip`).
- **Derived Last Share for firmware without `lastSubmittedDiff` / `lastShareSeconds` (NerdOCTAxe / NerdQAxePlus v1.0.37.1):**
  - Tracks `sharesAccepted` (or the summed `stratum.pools[].accepted`) per device across polls and records the wall-clock time whenever the count increases.
  - Displays difficulty as `≥ <poolDifficulty>` (every accepted share is at least the pool difficulty) plus the time ago, e.g. `≥ 12,173 · 15s ago`.
  - Before the first observed increase, the card shows `waiting for share…` instead of an empty Last Share.
  - Tracking state is persisted in `userdata/last_share.json` so it survives dashboard restarts and device renames; a miner reboot (accepted-count decrease) resets cleanly.
  - Firmware that already reports `lastSubmittedDiff` / `lastShareSeconds` (hexOS / some AxeOS builds, Braiins OS+) is unchanged and still preferred.
- **Richer NerdOCTAxe v1.0.37.1 telemetry:**
  - Second fan from `fanrpm2` / `fanspeed2` or `fans[]` (`rpm`, `speedPerc`, `label`).
  - `hashRate_1d` shown as the 1d hashrate on the card.
  - Ping RTT and loss (`lastpingrtt` / `recentpingloss`, or the connected pool's `pingRtt` / `pingLoss`).
  - `FALLBACK` badge from `stratum.usingFallback` as well as `isUsingFallbackStratum`.
  - `bestDiff` / `bestSessionDiff` (and `stratum.totalBestDiff` / pool `bestDiff` fallbacks).
  - `currentA` preferred for input current; `maxPower` used as the power limit when present.
  - Still does **not** display misleading `foundBlocks` / `totalFoundBlocks`.
- **Unchanged safety rules:** NerdOCTAxe autotune remains frequency-only (never changes voltage automatically), the frequency cap is **850 MHz**, and VR temperature is `max(vrTemp, vrTempInt)`.

## v1.4 (2026-11-22)
- **Versioning (`VERSION` = `1.4`):** Release zip `miner-autotune-v1.4.zip` (and refreshed `miner-autotune.zip`).
- **Fleet Summary Bar:** Live aggregate header showing machines online/total, total fleet hashrate, total power (W), fleet efficiency (`J/TH`), hottest chip temp + device, hottest VR temp + device, and best share ever + device.
- **Braiins OS+ (`bosminer-plus-tuner` / `BOSer`) full monitoring for Antminer S19:**
  - Automatically detects Braiins OS+ from `STATUS.Description` (`BOSer` / `bosminer`) while keeping stock BMMiner / VNish / LuxOS CGMiner fallback intact (monitor-only; no S19 auto-adjustments).
  - Queries `summary`, `devs`, `temps`, `fans`, `tunerstatus`, and `pools` on separate CGMiner TCP `:4028` connections:
    - Per-board table (5s/15m/avg hashrate, nominal MHS, chip & board temps from `TEMPS[]`, ignoring `DEVS[].Temperature = 0`, HW errors, board status / `TunerChainStatus`).
    - All 4 fans (RPM and Speed %), power vs `PowerLimit`, `J/TH`, `TunerMode`, uptime, accepted/rejected (`Pool Rejected%`)/stale/HW errors, and best share (explicitly omits misleading `Found Blocks`).
    - Priority-ordered pool list (all 7 pools, active pool highlighted).
- **hexOS v2.13.1-hexos.1 (AxeOS-compatible) rich telemetry (with graceful degradation for NerdOCTAxe v1.0.37.1 & stock AxeOS):**
  - Live/1m/10m/1h hashrate vs expected (`%`), watts & `J/TH`, chip/VR/board temps (`temp2 = -1` ignored), fan RPM & `%`, frequency, set & actual mV, HW error %, per-ASIC domain mini bars (`hashrateMonitor.asics[].domains[]`), accepted/rejected shares with `sharesRejectedReasons[]`, session & all-time best difficulty, WiFi RSSI + quality label, uptime, disconnects, block height, and `FALLBACK` stratum badge.
- **Last Share on every card:** Displays last share difficulty and relative age (`2,160 · 15s ago`) using `lastSubmittedDiff`/`lastShareSeconds` on AxeOS/hexOS and active pool/board `Last Share Difficulty`/`Last Share Time` vs `STATUS.When` on S19.
- **24h Trend Sparklines (`userdata/history/`):** Bounded per-device 24-hour hashrate and temperature history persisted under `userdata/history/` (atomic writes, survives updates and renames), rendered with inline SVG sparklines (no external CDN).

## v1.3 (2026-11-21)
- **Versioning (`VERSION` = `1.3`):** Displayed in the console startup banner, dashboard header (`Miner Autotune v1.3`), `/api/status`, and release zip name (`miner-autotune-v1.3.zip`). Future releases increment the minor version (`1.4`, `1.5`, ...).
- **Empty shipped `config.json`:** Removed all preconfigured/hardcoded miners from `config.json`. Add miners exclusively via **Scan network** → **Add** (newly added devices still default to autotune **OFF**).
- **Separate persistent user data (`userdata/devices.json`):** Discovered devices, custom names, enabled flags, manual frequency/voltage settings, and dashboard prefs (`dry_run`, `scan_subnet`) are stored in `userdata/devices.json` (with atomic writes and automatic `.bak` backups) — **never included in release zips**.
- **Automatic v1.2 → v1.3 migration:** On first start of v1.3, any existing devices in `config.json` and flags in `autotune-state.json` are automatically migrated into `userdata/devices.json` once, and future updates will never erase your saved miners.
- **Manual Frequency & Core Voltage control (when autotune is OFF):**
  - Editable **Frequency (MHz)** and **Core Voltage (mV)** inputs + **Apply** button on tunable cards (`gamma`, `nerdoctaxe`, `axeos-generic`) when autotune is stopped; locked/disabled while autotune is running.
  - Validated against limits (`freq <= 850 MHz`, `min_voltage_mv <= voltage <= 1250 mV` hard cap).
  - **NerdOCTAxe:** manual core voltage is allowed while autotune is OFF (clamped to the **1250 mV** hard cap with a warning); automatic tuning remains strictly frequency-only and never alters NerdOCTAxe voltage.
  - **Antminer:** remains monitor-only on stock firmware (no manual freq/voltage).
- **Resume autotune from manual settings:** Clicking **Start autotune** reads the current device frequency/voltage (falling back to saved manual values), resets cooldown and poll streaks, and starts tuning from those values instead of snapping back to prior tuner state.
- **Per-device Pool Editor:**
  - AxeOS / NerdOCTAxe: edit primary & fallback stratum URL, port, worker/user, and password (passwords are never displayed back and blank passwords leave stored passwords untouched) via `PATCH /api/system` + confirmed miner restart (`POST /api/system/restart`).
  - Antminer: attempts CGMiner `privileged` / `addpool` / `switchpool`; if stock Bitmain firmware rejects writes (`Access denied`), displays a clear explanation (`--api-allow W:<PC-IP>` or change pools in the Antminer web UI).

## v1.2
- HW error-driven Gamma core voltage (`1210 mV` start, `±10 mV`, `1150 mV` floor, `1250 mV` hard cap), frequency up to `850 MHz`.
- NerdOCTAxe v1.0.37.1 hardening (`/api/system/info` priority, ignores `0`-valued `asicTemps[]`, `vr = max(vrTemp, vrTempInt)`, `--probe` / `probe.bat`, unparsed card state).
- Antminer S19 Pro CGMiner `:4028` monitoring, inline device renaming, LAN `/24` discovery with newly added miners defaulting to autotune **OFF**.

---

## Updating from v1.2 (and future v1.4+ updates)

## Upgrading from v1.2 to v1.3 (preserving your existing miners)
1. **Stop** `run.bat` (Ctrl+C) or the scheduled task (`Stop-ScheduledTask -TaskName MinerAutotune`).
2. Extract the new files **except** keep your existing `config.json` and `autotune-state.json` in place for the first launch (or simply overwrite the `.py` / `.bat` / `.ps1` / `VERSION` / `README.md` files without deleting your old `config.json` yet).
3. Start `run.bat` (or `run-dry.bat`) once: v1.3 automatically reads your existing `config.json` + `autotune-state.json` and writes **`userdata/devices.json`** (plus `userdata/devices.json.bak` on subsequent edits).
4. Once `userdata/devices.json` exists, you can replace `config.json` with the clean shipped `config.json` (`"devices": []`) at any time — `miner-autotune` reads your devices from `userdata/devices.json`.

## Upgrading from v1.3 to v1.4+ (source / run.bat)
1. Stop `run.bat` / the scheduled task.
2. Extract `miner-autotune-v1.X.zip` directly over your `miner-autotune` folder. Because `userdata/` is **not** inside the zip, your `userdata/devices.json` (miners, names, enabled flags, manual settings, prefs) and `logs/` are **untouched**.
3. Start `run.bat` / the scheduled task again.

---

## Desktop app (NonceKeeper.exe)

## First launch: Windows SmartScreen
The exe is not code-signed, so Windows may show **"Windows protected your PC"**. Click **More info → Run anyway** (only needed once). Some antivirus tools flag unsigned PyInstaller programs; if yours quarantines it, allow `NonceKeeper.exe`, or keep using `run.bat`.

## Requirements
- Windows 10/11 64-bit. The app window uses the **Microsoft Edge WebView2 Runtime**, already installed on up-to-date Windows 10/11. If it is missing, the app tells you and uses your browser; install WebView2 from <https://go.microsoft.com/fwlink/p/?LinkId=2124703>.
- No Python install is needed for the exe. Internet access is optional (network stats, price, block confirmation, firmware notices); without it those panels just stay empty.

## Where things are
- `NonceKeeper.exe`, `config.json`, `userdata\`, `logs\` all in the same folder.
- `logs\autotune.log`: the app log (open it if something looks wrong).
- `userdata\nonce_keeper.json`: app settings (alerts, sounds, power, schedules, LAN, block watch, coins, quiet mode, restarts, webhooks, theme, layout). `nonce_keeper.json.v1.bak` is the pre-v1.9 copy.
- `userdata\session_best.json`, `pool_stats.json`, `chip_health.json`, `restarts.json`: v1.9 trackers (safe to delete; they start over).
- `userdata\fault_records\<device>_<timestamp>.json.gz` and `userdata\fault_buffers\`: automatic 12 h pre-fault captures (kept until manually deleted) and rolling per-device poll buffers.
- `userdata\fault_history.json`, `userdata\fault_context.json`, `userdata\fault_snapshots.json`, `userdata\tuner_safety.json`, and `userdata\log_snapshots\`: fault timeline (last 2,000 events), remembered VR overheat context, pre-fault snapshots, post-emergency lockout state, and captured miner logs (256 KB cap per snapshot). `nonce_keeper.json.v3.bak` and `devices.json.v2.bak` are automatic pre-v1.12 backups.
- **Data log retention:** `logs\autotune.log` rotates at 5 MB × 5 backups (no day-based expiry); `userdata\history\` keeps 24 h raw samples, 7 days at 5-min buckets, and 30 days at 30-min buckets; `userdata\tuning_log.json` keeps the last 2,000 events; `userdata\fault_history.json` keeps the last 2,000 fault events.
- Closing the window stops everything. To stop an autostarted app, close its window.

## Phone / LAN access
Settings → **Phone / LAN Access** → tick *Allow other devices*, pick a port (default 8081), set a PIN, and decide whether LAN devices are read-only (default). The settings box shows the URL to open on your phone (e.g. `http://192.168.1.20:8081`). Windows Firewall may ask the first time: allow **Private networks** only.

## Block-found confirmation
Settings → **Block-Found Confirmation** → paste the BTC address your solo pool pays the coinbase to; for other coins use Settings → **Coins** (one list per coin). When a share reaches that coin's network difficulty, Nonce Keeper checks the coin's explorer and only celebrates when a new block's coinbase pays one of your addresses. If you mine on a regular (non-solo) pool, the coinbase pays the pool, so a "block" will never confirm — that is intended.

## Rebuilding the exe (optional)
Install 64-bit Python 3.11 or 3.12, then double-click `build-exe.bat`. It creates a private `.venv-build` folder, installs PyInstaller and pywebview there, and writes a new `NonceKeeper.exe` next to it.

## Still prefer the browser or a headless service?
`run.bat`, `run-dry.bat`, `probe.bat` and `install-task.ps1` still work (all features included). Use either the exe or `run.bat` / the scheduled task, not both at the same time.

## Upgrading from v1.5 or older
Extract the zip over your folder exactly as above; `userdata\` (v1.3+) or your old `config.json` + `autotune-state.json` (v1.2) are migrated automatically on first start.

---

## 1. Install Python on Windows 10

1. Download the latest **Python 3** Windows installer from <https://www.python.org/downloads/>
2. Run it.
3. On the first screen, tick **Add python.exe to PATH**.
4. Click **Install Now**.
5. Open a new Command Prompt and check:

```bat
py -3 --version
```

You want 3.9 or newer. No `pip` packages are required.

Extract `miner-autotune-v1.6.zip` onto that PC, for example:

```
C:\Users\<you>\miner-autotune\
```

Keep `autotune.py`, `dashboard.py`, `scanner.py`, `VERSION`, `config.json`, and the `.bat` / `.ps1` files in the same folder.

---

## 2. First run & adding miners via LAN Scan

1. In File Explorer, open the `miner-autotune` folder.
2. Double-click `run-dry.bat` (recommended first) or `run.bat`.
3. Open **http://localhost:8080** in your browser.
4. On a fresh install, no miners are preconfigured. Click **Scan network** at the top right, optionally enter your subnet (or leave blank to auto-detect your local `/24`), and click **Run scan**.
5. Click **Add (autotune OFF)** on each discovered miner (Bitaxe Gamma 601, NerdOCTAxe, or Antminer S19 Pro). Every added miner is saved to `userdata/devices.json` and starts with autotune **OFF**.
6. Click **Rename** on any card to customize its label, **Apply** under Manual Control while autotune is OFF to test or set custom MHz / mV, or **Edit pool** to change stratum pool settings.
7. Click **Start autotune** when ready to hand control back to autotune (resumes directly from the miner's current / manually set frequency & voltage).

Logs are written under `logs\` next to the script:
- `logs\autotune.log` — human-readable log
- `logs\autotune.csv` — one row per poll, easy to open in Excel

The dashboard is served only on this PC (`127.0.0.1:8080` by default). Never expose it directly to the public internet.

## Remote Access with Tailscale
1. **Preferred — `tailscale serve` (keeps Nonce Keeper localhost-only):**
   - Keep **Phone / LAN / Tailscale Access** turned **off** in Settings so Nonce Keeper only listens on `127.0.0.1:8080`.
   - On the PC running Nonce Keeper, run:
     ```powershell
     tailscale serve --bg 8080
     ```
     (Replace `8080` if you use a custom port.) Your dashboard is then available over HTTPS at your machine's Tailscale MagicDNS URL, restricted to your tailnet. Set a PIN in Settings if you also want a PIN prompt over `tailscale serve`.
2. **Alternative — Built-in Tailscale / LAN listener:**
   - Open **Settings → Phone / LAN / Tailscale Access**, enable the remote access listener, choose **Tailscale only (`100.x` tailnet IP)** (or **All LAN & Tailscale (`0.0.0.0`)**), set a **4–12 digit PIN** (strongly recommended), and optionally untick **Read-only** if you want to make changes remotely.

---

## 3. Start automatically at boot (Task Scheduler)

1. Close `run.bat` if running (Ctrl+C).
2. Open PowerShell **as administrator** and `cd` into your `miner-autotune` folder.
3. Allow the script in this window:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   ```
4. Run:
   ```powershell
   .\install-task.ps1
   ```
5. Enter your Windows sign-in password when prompted, then optionally start immediately:
   ```powershell
   Start-ScheduledTask -TaskName MinerAutotune
   ```

Useful commands:
```powershell
Get-ScheduledTask -TaskName MinerAutotune | Get-ScheduledTaskInfo
Start-ScheduledTask -TaskName MinerAutotune
Stop-ScheduledTask  -TaskName MinerAutotune
.\uninstall-task.ps1
```

---

## How autotune decides

Every `poll_interval_sec` (default 15 s) the script polls each device and computes a rolling HW error rate window (default 5 polls) on tunable miners.

| Priority | Condition | Action |
|---|---|---|
| 1 | ASIC >= max (`65 °C`) or VR >= max (`70 °C`) | **Emergency frequency step-down** (`50 MHz`). Bypasses cooldown. **Never raises voltage.** |
| 2 | ASIC > target (`60 °C`) or VR > target (`65 °C`) | **Frequency step-down** (`25 MHz`), respecting cooldown. **Never raises voltage.** |
| 3 | Gamma only: rolling HW error > upper threshold (`2.0%`) and temps <= target | Raise voltage `+10 mV` up to **1250 mV hard cap**; if already at `1250 mV`, step frequency down `25 MHz`. |
| 4 | Both temps <= target − hysteresis (`57 °C` ASIC / `62 °C` VR) for `N` cool polls **AND** HW error <= `2.0%` | **Frequency step-up** (`25 MHz`), up to **`850 MHz`**. |
| 5 | Gamma only: rolling HW error < lower threshold (`0.5%`) for `N` polls (default `5`) | Lower voltage `−10 mV` down to `min_voltage_mv` (`1150 mV`). |
| 6 | Antminer (`type: "antminer"`) | **Monitor only** — CGMiner TCP `:4028` telemetry. |

---

## Command line & NerdOCTAxe probe

```bat
run.bat
run.bat --dry-run
run.bat --dry-run --once
run.bat --once
run.bat --no-dashboard
run.bat --dashboard-port 8081
run.bat --dashboard-host 0.0.0.0
run.bat --open-browser
run.bat --config C:\path\to\config.json
run.bat --log-dir D:\miner-logs
probe.bat
run.bat --probe 192.168.1.100
```

---

## Safety

- Frequency is clamped to `min_freq`–`max_freq` (`850 MHz` max for Gamma; **800 MHz** normal max for NerdOCTAxe unless `overclock_opt_in` is enabled per machine).
- Gamma core voltage is clamped between `min_voltage_mv` (`1150 mV`) and the **1250 mV hard cap**, and autotune never raises voltage when ASIC or VR temp is above target.
- NerdOCTAxe core voltage is **never changed by autotune**; manual voltage changes while autotune is stopped are clamped to **1250 mV** with a warning.
- Antminers are strictly monitor-only for frequency/voltage; remote CGMiner pool writes only work if the miner's firmware enables `--api-allow W:<your-PC-IP>`.
- **Fault Clear/Reset (v1.10)** is manual only and asks first. It is blocked while the miner is offline, while it is hot (cooldown limits for clear/resume/overheat restarts, your max temps for any other action) and by a rate limit (default one per 10 min, 4 per day per machine). Dry-run only logs. Braiins OS+ is never factory-reset; stock Bitmain reboot requires a web password you saved yourself (no default credentials are tried). Web logins can only be changed from this PC, and LAN read-only mode blocks Clear/Reset.

| Firmware | Fault | Clear/Reset action | Waits for cooldown |
| --- | --- | --- | --- |
| AxeOS | `overheat_mode` | `PATCH /api/system {"overheat_mode":0}` | yes |
| AxeOS | `miningPaused` | `POST /api/system/resume` | yes |
| AxeOS | `power_fault` / `hardware_fault` | `POST /api/system/restart` | no (max temps) |
| NerdQAxe+ | `boardError` 1–2 (overheat) | restart | yes |
| NerdQAxe+ | other `boardError` / `shutdown` | restart (OTP code if enabled) | no (max temps) |
| Braiins OS+ | paused / thermal pause | Resume mining | yes |
| Braiins OS+ | error code | Restart (never factory reset) | no (max temps) |
| Stock Bitmain | chain / fan / log errors | `GET /cgi-bin/reboot.cgi` (HTTP Digest, saved login only) | no (max temps) |
