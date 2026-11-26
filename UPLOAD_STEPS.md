# Publishing Miner Hearth v1.17.0 on GitHub

Nothing here has been uploaded yet. Replace `<github-user>` with your GitHub user or organisation and `<repo>` with the repository name everywhere below. The suggested repository name is **`Miner-Hearth`**. The `repo/` files use the same placeholders: replace them before you push (one command in step 2).

The same `repo/` contents are also in `Miner-Hearth-repo-files-v1.17.0.zip` (its root is the repository root), handy for GitHub's web upload.

## 1. Repository: rename the old one, or start fresh

**A. Rename the existing repository (keeps stars, issues and old releases).**
1. Open the old repository → **Settings → General → Repository name**, type `Miner-Hearth`, click **Rename**. GitHub redirects the old URL to the new one.
2. In your local clone: `git remote set-url origin https://github.com/<github-user>/Miner-Hearth.git`.
3. Optional: rename the user / organisation too (**Settings → Account** / organisation settings). Old links to the user name are *not* redirected for long, so only do this if you must.

**B. Fresh repository.** **New repository** (the **+** menu), owner `<github-user>`, name `Miner-Hearth`, **Public**, and leave "Add a README", ".gitignore" and "license" **unticked**. Click **Create repository**.

## 2. Push the source (`repo/` folder)

Fill in the placeholders first (in the `repo` folder; Git Bash on Windows, or Linux / macOS):

```bash
grep -rl --exclude=prepare.sh --exclude-dir=.git '<github-user>\|<repo>' . | xargs sed -i 's#ghcr.io/<github-user>/#ghcr.io/your-user-in-lowercase/#g; s#<github-user>#YOUR-USER#g; s#<repo>#Miner-Hearth#g'
grep -rn --exclude=prepare.sh '<github-user>\|<repo>' .     # should print nothing
```

`umbrel/` and `5tratumos/` still have `<store-repo>` and `<image-digest>`; `umbrel/prepare.sh` fills those in step 4 (don't edit `prepare.sh` itself). GHCR image names must be lower-case. Also check `LICENSE`: the copyright holder is "Miner Hearth contributors"; change it if you want your name there.

**Renamed repository (A):** copy the contents of `repo/` over your clone, then:

```bash
git add -A
git status            # check: no userdata/, logs/, *.exe or saved_pools.json in the list
git commit -m "Miner Hearth v1.17.0 (renamed)"
git push
```

**Fresh repository (B):**

```bash
git init
git add .
git status            # check: no userdata/, logs/, *.exe or saved_pools.json in the list
git commit -m "Miner Hearth v1.17.0"
git branch -M main
git remote add origin https://github.com/<github-user>/Miner-Hearth.git
git push -u origin main
```

No Git? On the repository page use **Add file → Upload files** and drag in the *contents* of `repo/` (folders such as `.github/` may need **Add file → Create new file** with the path typed in).

## 3. Create the release

1. **Releases → Draft a new release** (or `https://github.com/<github-user>/Miner-Hearth/releases/new`).
2. **Choose a tag** → `v1.17.0` → **Create new tag on publish**. Target: `main`.
3. **Release title**: `Miner Hearth v1.17.0`.
4. **Description**: paste `RELEASE_NOTES.md` (replace `<github-user>` there too).
5. **Attach binaries**: `MinerHearth-Setup-v1.17.0.exe`, `MinerHearth-Setup-v1.17.0.zip`, `MinerHearth-v1.17.0-portable.zip`, `miner-hearth-umbrel-v1.17.0.zip`, `miner-hearth-5tratumos-v1.17.0.zip` and `SHA256SUMS.txt`.
6. Tick **Set as the latest release** and click **Publish release**.

Publishing the tag also starts the **docker** workflow (Actions tab), which runs the tests and pushes `ghcr.io/<github-user>/miner-hearth:1.17.0` (amd64 + arm64).

## 4. Container image, Umbrel and 5tratumOS (optional)

1. When the **docker** workflow is green, copy the image digest from the run summary.
2. **Packages → miner-hearth → Package settings → Change visibility → Public**.
3. Make a second public repository for the app store, e.g. `<github-user>/miner-hearth-store`. In a copy of `repo/umbrel/` run `sh prepare.sh <github-user> miner-hearth-store Miner-Hearth sha256:<digest>`, then push the **contents** of `store/` to it. Umbrel users add `https://github.com/<github-user>/miner-hearth-store` under **App Store → ⋯ → Community App Stores**; 5tratumOS users can try it as a **Custom Store** (unverified).
4. 5tratumOS GLOBAL catalogue (optional): unzip `miner-hearth-5tratumos-v1.17.0.zip`, run its `prepare.sh` (same arguments; it also fills `global-submission/`), then open a pull request with `5tratumos/global-submission/miner-hearth/` against `WillItMod/5tratStore-global` (branch `app/miner-hearth`), leaving it `status: proposed`. The maintainers test and approve it.
5. If you rebuild the image, the digest changes: update the compose files and re-run `prepare.sh` on fresh copies.

## 5. Optional

- Repository **About** (gear icon): description "Dashboard and auto-tuner for home ASIC miners" and topics such as `bitaxe`, `mining`, `asic`, `antminer`, `braiins`, `solo-mining`, `umbrel`.
- **Settings → General → Features**: turn on Issues.
- Old releases keep their old file names; that's fine.
