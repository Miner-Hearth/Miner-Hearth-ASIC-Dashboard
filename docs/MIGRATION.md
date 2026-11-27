# Moving from Nonce Keeper to Miner Hearth (v1.17.0)

Miner Hearth v1.17.0 is the program that was called **Nonce Keeper** up to v1.16.3 (and Miner Autotune
before v1.8). It was renamed because "nonce" is an offensive slang word in British English; the mining term
is unchanged inside the program and in miner / pool data. This page is the only place (with the changelog)
that uses the old name.

| | Nonce Keeper (≤ v1.16.3) | Miner Hearth (v1.17.0+) |
| --- | --- | --- |
| Program | `NonceKeeper.exe` | `MinerHearth.exe` |
| Installer | `NonceKeeper-Setup-vX.exe` | `MinerHearth-Setup-vX.exe` |
| Install folder | `%LOCALAPPDATA%\Programs\Nonce Keeper` | `%LOCALAPPDATA%\Programs\Miner Hearth` |
| Settings file | `userdata\nonce_keeper.json` | `userdata\miner_hearth.json` |
| Sign-in entry (HKCU `...\Run`) | `NonceKeeper` | `MinerHearth` |
| Apps & features entry (HKCU `...\Uninstall`) | `NonceKeeper` | `MinerHearth` |
| Update packages | format `nonce-keeper-update` | `miner-hearth-update` (old format still accepted) |
| Backups / saved-pool exports | `nonce_keeper_backup`, `nonce-keeper-pools` | `miner_hearth_backup`, `miner-hearth-pools` (old ones still import) |
| Portable zip folder | `miner-autotune\` | `miner-hearth\` |

## What happens on the first start of Miner Hearth

`nk_legacy.migrate_userdata()` runs once, before anything else is loaded:

1. If the Miner Hearth folder already has its own data (`userdata\devices.json`, `miner_hearth.json`,
   `nonce_keeper.json` or `saved_pools.json`), nothing is copied.
2. Otherwise it looks for an old data folder, in this order: the folder the installer recorded
   (`HKCU\Software\Miner Hearth\LegacyDataDir`), the old installer's `InstallLocation`, the
   `NONCE_KEEPER_HOME` environment variable, `%LOCALAPPDATA%\Programs\Nonce Keeper`, and folders named
   `miner-autotune`, `Nonce Keeper` or `NonceKeeper` next to the Miner Hearth folder.
3. From the first one that has data it **copies** `userdata\` (machines, passwords you saved, settings,
   saved pools, history, fault history, event logs; not `userdata\updates\`, the old live-update files) and
   `logs\`. Existing files are never overwritten. `config.json` is copied only if the new one is still the
   shipped default.
4. `userdata\nonce_keeper.json` is copied to `userdata\miner_hearth.json` (also done whenever only the old
   settings file exists).
5. The result is written to `userdata\migration.json` and to the log. **The old folder is never changed or
   deleted**; delete it yourself once you are happy.

## The installer

`MinerHearth-Setup-v1.17.0.exe` has a component **Move from the old name (Nonce Keeper)**, ticked only
when an old install is found. It:

- asks to close `NonceKeeper.exe` if it is running;
- records the old folder for step 2 above;
- deletes only the old **program** files (`NonceKeeper.exe`, its `Uninstall.exe`, `VERSION`, `README.md`,
  `INSTALL.txt`, the autostart `.bat` files and the icon), the old Start Menu folder and Desktop shortcut,
  the old Apps & features entry, and moves the old sign-in entry to Miner Hearth;
- **keeps** `userdata\`, `logs\` and `config.json` in the old folder.

Untick it to leave the old install completely alone (then you can uninstall it later from Windows Settings
→ Apps; its uninstaller keeps your data unless you tick the delete box).

## Portable zip

Unzip `miner-hearth\` next to the old `miner-autotune\` (or `Nonce Keeper\`) folder, or extract it over the
old folder. Start `MinerHearth.exe`. If the old copy started with Windows, the sign-in entry is moved to
`MinerHearth.exe` on the first start. Delete `NonceKeeper.exe` afterwards; both share one single-instance
lock, so they can't run at the same time.

## Live updates

An old Nonce Keeper (v1.16.x) **cannot live-update to v1.17.0**: the program file and its main module were
renamed, and old programs don't accept the new package format. Install v1.17.0 once with the installer or
the zip. Miner Hearth still accepts `nonce-keeper-update` packages, and ignores any old live-update files
whose version is older than the program (so leftovers can't replace new modules).

## Other places

- The browser header `X-Requested-With: NonceKeeper` from old LAN pages is still accepted.
- The scheduled task (`install-task.ps1`, source version) keeps its task name `MinerAutotune`.
- GitHub: rename the repository (Settings → General → Repository name, e.g. `Miner-Hearth`); GitHub
  redirects the old URL. Old releases keep their old file names.
