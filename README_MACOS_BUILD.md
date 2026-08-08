# PassPoSys — macOS (Apple Silicon) build via GitHub Actions

This repo builds a native **macOS arm64** version of the PassPoSys Flask app
using GitHub Actions — you never need to own a Mac. Push to `main` (or run
the workflow manually) and download the finished `.zip` from the Actions
run's **Artifacts** section.

## ⚠️ Before you push anything to GitHub

Two files that were in your original Windows build folder contain **live
secrets** and must never be committed:

- `credentials.json` — Google OAuth client secret
- `token.json` — a live Google Drive OAuth access/refresh token

Both have been **removed** from this prepared copy. If you're merging this
into your own existing repo, double-check they aren't already committed in
your git history — if they are, treat that OAuth client/token as compromised
and rotate it in Google Cloud Console, since removing a file from a new
commit does not erase it from history.

`env.enc` and `token.enc` ARE safe to commit — they're already encrypted
with `passposys.key`, which is NOT in this repo and must never be. Set it
up as follows.

## One-time setup

1. **Add `passposys.key` as a GitHub Actions secret** (not a repo file):
   - Repo → Settings → Secrets and variables → Actions → New repository secret
   - Name: `PASSPOSYS_KEY`
   - Value: the contents of your existing `passposys.key` file (same one
     you already use on Windows — it's just bytes, not platform-specific)
   - Then add a step to the workflow that writes it to `passposys.key`
     before the build (see `.github/workflows/build-macos.yml` — you'll
     need to uncomment/add this step since it currently expects
     `passposys.key` to already exist in the checkout; using a secret is
     the safer option and is documented inline in the workflow file).

2. **`env.enc` / `token.enc`** — copy these straight from your Windows
   build folder into the repo root. They're already committed in this
   prepared copy since they were present in your uploaded zip and are
   encrypted-safe.

3. **`.env.example`** — for reference only, showing which variables
   `encrypt_env.py` expects. You don't need to run `encrypt_env.py` again
   on Mac since `env.enc` is reused as-is.

### What to actually do with `.env` right now

You do **not** need to create a `.env` file anywhere for this workflow to
work. Here's why, spelled out:

- Your real `.env` (with actual secret values) lives only on your Windows
  machine, where you already ran `encrypt_env.py` to produce `env.enc`.
- `env.enc` is what gets committed to this repo and shipped inside the
  Mac build — `launch.py` decrypts it at runtime using `passposys.key`,
  identically on Windows and macOS.
- `build_mac.sh` deliberately does **not** call `encrypt_env.py` in CI,
  because there's no `.env` file on the GitHub Actions runner (and there
  shouldn't be — it's gitignored, as it should only ever exist locally on
  a machine you control).

**So: leave `.env` alone. Never add it to the repo. Just make sure
`env.enc` (already generated) sits in the repo root before you push.**

If you ever need to change a secret (e.g. rotate `HOST_API_SECRET`):
1. Edit `.env` on your Windows machine (the real one, not `.env.example`)
2. Run `python encrypt_env.py` there to regenerate `env.enc`
3. Copy the new `env.enc` into this repo and commit it
4. Push — the next Mac build will pick up the new secret automatically

## Repo layout

```
.
├── .github/workflows/build-macos.yml   ← the CI workflow
├── launch.py                            ← PATCHED: cross-platform MySQL/subprocess handling
├── backup.py                            ← PATCHED: cross-platform mysqldump resolution
├── app_routes.py                        ← PATCHED: cross-platform backup subprocess calls
├── build_mac.sh                         ← macOS analog of build.bat
├── start.sh / stop.sh                   ← macOS analog of start.bat / stop.bat
├── requirements.txt                     ← derived from import scan — VERIFY against pip freeze
├── env.enc / token.enc                  ← encrypted secrets, safe to commit
├── (everything else)                    ← unchanged from your Windows project
```

## What changed vs. your Windows code (see PATCH_NOTES.md for full detail)

- `launch.py`: MySQL binary names (`mysqld` vs `mysqld.exe`), removed the
  Windows-only `creationflags=0x08000000` on non-Windows, `/dev/null`
  instead of `NUL`, added a `chmod +x` step for the portable MySQL binaries
  (macOS strips the execute bit on extraction).
- `backup.py`: same `creationflags` fix; `mysqldump` binary search now
  checks Homebrew/pkg-installer paths on macOS instead of
  `C:\Program Files\...`.
- `app_routes.py`: same `creationflags` fix in the two manual "backup
  database" web-route subprocess calls.

Nothing else was touched. `config.py`, `app_core.py`, `db.py`, etc. already
used OS-agnostic `sys.frozen`/`sys.executable` patterns and needed no
changes.

## Out of scope (for now)

`visitvisa.py` (the separate PyQt6 "passposys.exe" desktop client with
embedded Chromium) is **not** built by this workflow. It has its own set of
macOS porting concerns (PyQt6 WebEngine on Apple Silicon, different
packaging flags) — tackle it as a follow-up once the Flask app is confirmed
working on Mac.

## Portable MySQL: how it's obtained

MySQL does publish an official "Compressed TAR Archive" for macOS — it's
just not surfaced on the main downloads *webpage* for newer 9.x releases,
only reachable through the direct mirror path
`dev.mysql.com/get/Downloads/...`. That path is script-fetchable with
`curl` (no browser/JS redirect), and is the same file a real Mac user
would end up with by picking "Compressed TAR Archive" during a manual
install. The workflow downloads
`mysql-8.0.46-macos15-arm64.tar.gz` directly, extracts it, and places the
resulting `bin/`, `lib/`, `share/` folders into a portable `mysql/` folder
next to the app — same layout your Windows build already uses. No
Homebrew, no system-wide install, no admin rights needed on the end
user's Mac; it behaves exactly like the Windows portable folder.

MySQL notes that "packages for Sequoia (15) are compatible with Sonoma
(14)" — GitHub's `macos-14` runner is Sonoma, so the `macos15-arm64`
tarball is confirmed compatible.

To track a newer MySQL release later, update `MYSQL_VERSION` and
`MYSQL_TARBALL_URL` at the top of `.github/workflows/build-macos.yml`.

## Running the workflow

- **Automatic**: push to `main`
- **Manual**: repo → Actions tab → "Build macOS (Apple Silicon)" → Run workflow
- Download the `PassPoSys-macOS-arm64` artifact from the completed run,
  unzip it, and you'll have:
  ```
  PassPoSys-macOS-arm64/
  ├── PassPoSys.app          ← double-click to run
  ├── mysql/                 ← portable MySQL (bin/lib/share)
  ├── env.enc, token.enc
  ├── passposys.key          ← only present if you wire up the secrets step
  ├── start.sh / stop.sh
  ```

## Known gaps to close before distributing to real Mac users

1. **`requirements.txt` needs verification.** It was derived from static
   import analysis (no `requirements.txt`/`Pipfile` shipped with your
   original project). Run `pip freeze` on your Windows venv and compare —
   version mismatches matter most for `opencv-python-headless`,
   `mysql-connector-python`, and `cryptography` since they wrap native
   code.
2. **Code signing / notarization.** An unsigned `.app` downloaded from the
   internet will be blocked by Gatekeeper on a real Mac ("app is damaged
   and can't be opened" / "unidentified developer"). Without an Apple
   Developer account ($99/yr) to sign and notarize, users will need to
   right-click → Open, or run
   `xattr -cr PassPoSys.app` in Terminal once, to bypass Gatekeeper. Worth
   documenting for your testers as a first step.
3. **No Mac available for actual testing.** This has been built and
   reasoned through carefully, but has not run on real Apple Silicon
   hardware. Treat the first CI-built artifact as a first testable
   candidate, not a finished release — budget time for at least one or two
   rounds of "download artifact → test on a friend's/borrowed Mac → report
   errors → patch" before distributing further.
