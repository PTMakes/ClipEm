# ClipEm maintainer tooling

ClipEm is a tool for clipping games. It ships
a preconfigured OBS with a replay buffer, a NSIS (Windows) and Python (Linux) setup/install wizard and an optional Python tray widget.
Everything lives in one repo, [PTMakes/ClipEm](https://github.com/PTMakes/ClipEm):

| Folder | Contents |
| --- | --- |
| `App/` | `App/Shared/`: everything shipped to users (setup, run, updater, tray widget, OBS config and scripts). |
| `Dev/` | Build, release and installer tooling. `Dev/obs-src` is a git-ignored clone of upstream OBS, made by `build-obs-from-source-for-linux.sh`. |
| `Website/` | clipem.app: the landing page and a copy of the signed update manifests for old installs. |
| `BugReportWorker/` | The Cloudflare Worker behind the tray's Send Bug Report button. See its [README](BugReportWorker/README.md). |

#### How to build/run a release:

## One-time setup

### Release signing key

```
Dev/generate-signing-key.sh
```

The script does three things:

- **Creates the private key** at `~/.config/clipem/signing-key.pem`
  (mode 600), outside every repo. Set `CLIPEM_SIGNING_KEY` to keep it
  somewhere else.
- **Stamps the public key** into
  `App/App/Shared/Installer/signing_public_key.txt` and
  `ClipEmSetupLinux.sh`.
- **Checks** that everything matches.

Commit the two stamped files in `App`. The Windows installer picks up the public key at build time.

Back the private key up offline, for example in a password manager.

`release.sh` refuses to run if the key is missing, or if any shipped copy of the public key doesn't match it.

### Tools

You need `git jq zip tar gzip curl openssl python3 makensis`, plus what the
builds below use: `x86_64-w64-mingw32-g++`, and for
[the OBS plugin](#clipem-obs-plugin) `g++ pkg-config clang-cl lld-link
llvm-dlltool llvm-readobj unzip cargo` and `qt6-base`. `release.sh` stops at
preflight if a tool is missing.

### GitHub CLI

`release.sh` creates the GitHub releases with [`gh`](https://cli.github.com).
Install it and run `gh auth login` once. `CLIPEM_RELEASE_REPO` overrides the
repo, which is `PTMakes/ClipEm` by default.

The repo must be **public**, because release downloads from a private repo
need a GitHub login.

Building Linux OBS or the portable Python also needs a C/C++ toolchain.
Those scripts are written for Arch/CachyOS.

Optional: to sign the Windows installer with Authenticode, install
`osslsigncode` and set `CLIPEM_CODESIGN_PFX` and `CLIPEM_CODESIGN_PASSWORD`.
Without this, SmartScreen warns users about an unrecognised app.

## Cutting a release

```
Dev/release.sh [--allow-dirty] [--no-upload] [--dev] [patch|minor|major|X.Y.Z]
```

Before you run it, add an entry for the new version at the top of
`App/App/Shared/release_notes.json`, with `version`, `date` and `sections`:
headings such as `New Features`, `Improvements` and `Fixes`, each with a list of
notes, up to 1500 characters in all. Give component updates their versions
(`Updated OBS from 32.2.2 to 32.3.0.`). A release stops at preflight without an
entry for its version. A dev build uses its coming version's entry, if there is one.

The entry goes into the signed `install-manifest.json`, so the update prompts in
`run.py` and Settings show it, and becomes the GitHub release's description. A
prompt runs the installed version's code, so installs from before this was added
don't show notes there. The file also ships with ClipEm: after an update, `run.py`
opens "ClipEm Has Updated From X to Y" with the notes of every version in between,
working out the old version from `last_version` in `update_config.json`, and
Settings > Release Notes shows the whole history.

1. **Preflight.** Checks the tools, the signing key, the version bump, the
   release notes, that
   the Windows and Linux Pythons share a minor version, and that the release
   isn't already uploaded. Runs `ruff` and `pytest` for `App` and
   `Dev/` (the GUI layout test is skipped without a display). If the repo has
   uncommitted changes, it asks for a commit message and commits them before
   building (`--allow-dirty` builds from them uncommitted instead).
2. **Build into `Dev/dist/vX.Y.Z/`.** This produces:
   - the shared zip (deterministic: sorted entries, fixed mtimes)
   - the Linux OBS tarball
   - the Xft-repacked Linux Python
   - `install-manifest.json` (it also links the ffmpeg source tarball, which
     clipem.app's "Source code & licenses" section shows)
   - the legacy `clipem-latest.json`
   - `ClipEmSetupWindows.exe`
   - the portable Linux bundle
   - `build-info.json`, which records the exact commits used

   The only changes to the source tree are `App/Shared/VERSION`,
   `App/Shared/checksums.json` and any new entries in
   `Dev/release/upstream-pins.json`.
3. **Sign both manifests.**
4. **Upload everything**, plus `ClipEmSetupLinux.sh`, to a *draft* GitHub
   release `vX.Y.Z`, after asking you first. The manifests go last.
5. **Copy to the website.** The signed manifests go into `Website/public`, for installs up to 1.3.5.
6. **Commit** "Update X.Y.Z", then ask whether to push `main` and publish the
   draft, which tags the release commit. Answer no to get the commands to
   run later instead.

Then deploy the website. `--no-upload` does a full dry run, stopping after
step 3.

### Testing dev builds

```
Dev/release.sh --dev
```

This builds `HEAD` the same way as a release. The version is
`<next patch>-dev.<UTC timestamp>`, for example `1.3.6-dev.202610142130`. The
build replaces every asset of the rolling `dev` prerelease, uploading the
manifests last, and deletes stale assets. It commits nothing, leaves the
website alone, and refuses to run with uncommitted changes unless you pass
`--allow-dirty`.

Installs whose Settings > Update channel is **Latest** get the dev build at
their next update check. A dev version sorts after the release before it and
before its own release, so `1.3.5 < 1.3.6-dev.* < 1.3.6`. Installs on
**Releases** never see it, because GitHub's `/releases/latest/` skips
prereleases. Switching a dev install back to **Releases** offers the current
release, even though it's older.

For a fresh install of the dev build on Linux:

```
CLIPEM_MANIFEST_URL=https://github.com/PTMakes/ClipEm/releases/download/dev/install-manifest.json ./ClipEmSetupLinux.sh
```









## How an install works

1. **The installer runs.** Windows users get `ClipEmSetupWindows.exe`
   (NSIS + `installer/windows/bootstrap.ps1`). Linux users run
   `App/Shared/Installer/Scripts/ClipEmSetupLinux.sh`, uploaded as an asset of
   each release (`clipem.app/linuxinstall.sh` redirects to the latest one).
2. **The manifest is checked.** The installer fetches the latest GitHub
   release's `install-manifest.json` and its `.sig`, then
   checks the Ed25519 release signature against the public key it ships
   with.
3. **Downloads are checked.** Only after the signature checks out does it
   download what the manifest lists (OBS, a portable Python, the shared
   zip) and check each file's sha256.
4. **Setup takes over.** `App/Shared/Installer/setup.py` handles everything
   from there: the wizard, the OBS config, the tray widget and autostart.
5. **Launching.** `run.py` only ever starts ClipEm's own OBS build. On
   Windows and Linux, if it's missing, `run.py` downloads it first
   (`obs_install.py`, the same verified download Setup uses). If that fails,
   it shows an error and stops. It never falls back to a system-wide OBS.
6. **Updates.** On every launch, `run.py` asks `updater.py` for a newer
   release. The updater makes the same signature and sha256 checks and
   applies the update atomically, rolling back on failure.

Every release is a GitHub release of PTMakes/ClipEm (tag `vX.Y.Z`). Its
assets are all the binaries plus the signed manifests, and the manifests'
URLs point at those assets. GitHub's limit is 2 GiB per asset, with no limit
on total size or bandwidth. `clipem.app/ClipEmSetup.exe` redirects to the
latest release's `ClipEmSetupWindows.exe`.

The updater reads `https://github.com/<source repo>/releases/latest/download/install-manifest.json`.
In the tray widget's Settings, users can change:

- **Update channel.** *Releases* (the default) or *Latest*, which reads the
  rolling `dev` prerelease instead (see [Testing dev builds](#testing-dev-builds)).
- **Update source.** The GitHub repo, `PTMakes/ClipEm` by default. Settings
  explains and asks before changing it. Another repo only works if its
  manifests are signed with ClipEm's release key.

Installs prior to 1.3.6 only look for `clipem.app/updates/install-manifest.json`
(and the even older `clipem-latest.json`). So `release.sh` keeps copying the
signed manifests to the website. Those installs read it there, download the
new version from GitHub and then update to read the GitHub themselves after that.
Old R2 objects at `downloads.clipem.app` are left alone, so old links keep
working.


### Where versions come from

| What | Source of truth |
| --- | --- |
| ClipEm version | the bump applied to the live `install-manifest.json` version |
| Windows/macOS OBS, Windows Python | `WINDOWS_OBS_VERSION`, `MACOS_OBS_VERSION`, `WINDOWS_PYTHON_VERSION` and `WINDOWS_PYTHON_PBS_TAG` in `setup.py` (and `Setup_Windows.bat`) |
| pip (Windows bootstrap) | `WINDOWS_PIP_VERSION` in `release.sh` |
| Linux OBS | the build in `Dev/obs-install` (`libobsConfigVersion.cmake`) |
| Linux Python | `LINUX_PYTHON_VERSION` and `LINUX_PYTHON_PBS_TAG` in `release.sh` |

Both platforms run the same python-build-standalone build (release.sh
checks). Windows' bundled Python is also the one OBS runs ClipEm's scripts
with, and OBS only loads Python up to 3.12, so that's the ceiling - and
python.org's last 3.12 Windows build is 3.12.10, so Windows takes PBS's too
(`windows_python.py`). ClipEm needs Python 3.11+ (Pillow, numpy) - for
macOS's system Python; older Windows installs get their Python replaced.

### Upstream pins (`upstream-pins.json`)

Every third-party download is pinned by sha256 in `upstream-pins.json`.

- **New pins:** where the publisher lists a digest, the pin is taken from
  it: GitHub's asset digest or the OBS release notes, PyPI, or PBS's
  `SHA256SUMS`. It is checked against that digest again on later releases.
  python.org and the Tcl/Tk sources publish no easily fetched digest, so
  those fall back to hashing the first download. The tooling prints a
  warning when that happens.
- **Mismatches:** a mismatch stops the release. Investigate it; don't just
  delete the pin.

## Linux OBS build

Upstream ships no portable Linux build, so `build-obs-from-source-for-linux.sh`
builds a relocatable one into `Dev/obs-install` (ignored by git and
published as a release asset). Details:

- **Pinned commit.** It builds tag `32.2.2`, pinned to its commit. A moved
  tag stops the build.
- **Pacman.** It doesn't refresh pacman's database. Run `sudo pacman -Syu`
  yourself first.
- **Clean checkout.** It resets `obs-src` to a clean checkout, then applies
  five patches for newer toolchains and for relocation (see the script).
  Each patch has to apply, or the build stops right away.
- **ABI.** It links against the build machine's Qt6/FFmpeg. Other
  machines need ABI-compatible libraries.
- **Disabled features.** Browser source, AJA and NVENC are off.

## ffmpeg (clip re-export)

`App/Shared/Scripts/clip_reexport.py` is an OBS script. When the setup
wizard's "Also save a smaller copy of each clip" option is on, it writes
`<clip>-small.mp4` next to every saved clip: the last N seconds, re-encoded
to fit a target size. The original clip is never changed.

`build-ffmpeg.sh` builds the ffmpeg it uses from official source: FFmpeg
`n9.0.2` and x264 `stable`, both pinned to exact commits.

- **Output:** static binaries for Linux (native) and Windows (mingw-w64
  cross-compile), plus a source tarball. The GPL requires publishing the
  source alongside the binaries.
- **Size:** the build is minimal (H.264/AAC in MP4 only, no network),
  around 4 MB per platform.
- **Caching:** builds are cached in `Dev/dist/ffmpeg/`, and `release.sh`
  runs the script automatically.
- **Requirements:** `sudo pacman -S --needed base-devel git nasm mingw-w64-gcc`.
- **Installing:** installers put it in `App/Windows/ffmpeg` or
  `App/Linux/ffmpeg`. On macOS, `brew install ffmpeg` is needed.

**Split audio tracks and app tracks.** With "Split audio tracks" on, OBS
records Mix, Mic and Desktop (tracks 1-3). The tray widget records the apps
itself (`app_recorder.py`; `parec` on Linux, `clipem-app-capture.exe` on
Windows 10 2004+) and, when a clip is saved, `tray_tracks.py` lines them up
with the clip's Desktop track and writes `<clip>-tracks.mkv` with one extra
track for every app that made sound during the clip, named after the app
(nothing to configure; the order is the order the apps were first heard),
then strips the clip itself back to the mix. Where the tray can't record
apps (macOS), `clip_reexport.py` splits the three OBS tracks itself.
`build/build-app-capture.sh` cross-compiles the Windows helper from
`Dev/app-capture/` into `App/Shared/Tools/` (`release.sh` runs it).

**Post-clip.** With post-clip on in Setup or Settings > Clip Output, the clip hotkey moves
from OBS's "Save Replay" to `Scripts/post_clip.py` and the replay buffer is kept
N seconds longer than the clip. A press plays the clip sound, and N seconds
later saves once. That save becomes the usual clip, ending at the press, plus
`<clip>-extra.mkv` with every track and the N seconds after it, with the clip
sound taken back out of its Mix and Desktop tracks. The details are at the top
of `Scripts/clip_post.py`.

To move to a newer FFmpeg, update `FFMPEG_TAG` and `FFMPEG_COMMIT` (and
optionally `X264_COMMIT`) at the top of the script, then run it with `--force`.

## ClipEm OBS plugin

`Dev/obs-plugin/clipem-obs.cpp` hides OBS (its tray icon, and its window when
there's no tray to hide to) and passes OBS's errors and messages on to the tray
widget (`tray-widget/obs_notices.py`). `run.py` loads it through
`OBS_PLUGINS_PATH`, so it ships in the shared zip, not inside OBS.

`build/build-obs-plugin.sh` builds it into `App/Shared/OBS-Plugin/` (ignored by
git), and `release.sh` runs it:

- **Linux:** built against `Dev/obs-install` and this machine's Qt6, so it has
  the same ABI caveat as [the Linux OBS build](#linux-obs-build).
- **Windows:** cross-compiled with `clang-cl` against the official OBS release
  and its own Qt. Everything needed is downloaded once into
  `Dev/.obs-plugin-work`. Running the script accepts Microsoft's license for
  the MSVC CRT and Windows SDK (via `xwin`).
- **Moving to a newer OBS:** update `OBS_WINDOWS_VERSION`, its sha256 and
  `QT_DEPS_VERSION` at the top of the script together with setup.py's
  `WINDOWS_OBS_VERSION`. A test stops the release if the two versions differ.
- **Requirements:** `sudo pacman -S --needed clang lld llvm qt6-base`, plus
  rustup/cargo, curl, unzip and a built `Dev/obs-install`.

## Portable Python for Linux

`repack-linux-python-xft.sh` rebuilds python-build-standalone's Tk with
Xft. Without Xft, the setup wizard renders tiny bitmap fonts. PBS releases
from `20260113` on bundle Tcl/Tk 9.0: for those only Tk is rebuilt, at the
exact version PBS ships, without CUPS, XScreenSaver or the system libtommath,
and the script fails if the result links anything beyond X11/Xft/fontconfig,
zlib and libc. Older releases bundle 8.6.14, which gets replaced with 8.6.16
(8.6.14's Xft point sizes render too large). With a display, the script also
checks that a 16pt font really renders at 16pt.

## Tests and CI

| Folder | Checks |
| --- | --- |
| `App` | `ruff check .` |
| `Website` | `npm run lint`, `npm run typecheck`, `npm run build`, and that each manifest has a signature. |
| `Dev` | `ruff check` (`Dev/ruff.toml`), `python3 -m pytest tests` (all tests, App and Dev, live in `Dev/tests/`; the tests that need a display run under `xvfb-run` in CI). `release.sh` preflight also runs these, and checks that the Python hash in `Setup_Windows.bat` matches `upstream-pins.json`. On a desktop that doesn't give the test window keyboard focus, the entry shortcut test skips itself. |
| `BugReportWorker` | `npm test`: the upload checks against zips from Python's `zipfile`. |

The workflows are in `.github/workflows/`: `app.yml` (App + Dev),
`website.yml` and `bug-report-worker.yml`. Each runs only when its folders
change.

### OBS script tests

`Dev/tests/test_obs_scripts.py` runs every OBS script through
its lifecycle against a stand-in `obspython` that only has the names the real
one has. Those names are in `Dev/tests/obspython_api.json`, generated from the
Linux OBS build by `Dev/generate_obspython_api.py`. Re-run that script after
rebuilding OBS; `release.sh` stops if the snapshot is out of date.
