# Developing agent-farm

The monitor is a Python [Textual](https://textual.textualize.io) program. The Mac app is a
thin native shell around it: a window with a [SwiftTerm](https://github.com/migueldeicaza/SwiftTerm)
terminal that runs a PyInstaller build of the Python code, plus
[Sparkle](https://sparkle-project.org) for updates. Everything the monitor does happens in
Python, so day-to-day development never touches Swift or the bundle.

## Layout

| path | what |
|------|------|
| `main.py` | the Textual app: tree, detail pane, fleet line, usage panel, settings screen |
| `collector.py` | the passive data layer: registry, transcripts, subagents, git, `it2`; `python collector.py` prints a one-shot dump |
| `usage.py` | incremental transcript scanner behind the usage panel |
| `ordering.py` | the orderings; an `Ordering` is a tuple of `Tier`s, each with a `match` predicate and a sort `key`; add one and append it to `ORDERINGS` |
| `settings.py` | persisted settings; a new setting is a dataclass field with a default |
| `apppaths.py` | where logs and settings live: next to the code from a checkout, under `~/Library` in the app |
| `supervisor.py`, `supervisor.sh` | development runner that restarts `main.py` when a `.py` file changes |
| `macos/` | the SwiftPM package for the host app (`Package.swift`, `Sources/AgentFarm`, `Info.plist`) |
| `packaging/` | the PyInstaller spec and the hardened-runtime entitlements for the Python side |
| `scripts/` | `build_app.sh`, `sign_app.sh`, `release.sh`, `fetch_sparkle_tools.sh`, `make_icon.swift` |
| `.github/workflows/release.yml` | the same release, run on GitHub's macOS runner |
| `VERSION` | the single source of the version number |

## Running from source

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    ./supervisor.sh                  # runs main.py and hot-reloads it on any .py change
    .venv/bin/python main.py         # plain run
    .venv/bin/python main.py --usage --order activity   # one-run overrides of the saved settings

From a checkout the settings file is `monitor_settings.json` next to the code (or
`CLAUDE_MONITOR_SETTINGS`), and logs go to `logs/monitor.log` and `logs/supervisor.log`.
`CLAUDE_MONITOR_ORDER` and `CLAUDE_MONITOR_USAGE=1` do what the flags do.

The supervisor ignores `.venv`, `build`, `dist`, `.build` and `.sparkle-tools`, so building
the app does not restart the monitor you are looking at. It restarts the child only; edits
to `supervisor.py` itself need a manual restart. The child's stderr is deliberately not
captured, because Textual draws on stderr; crashes are logged by `main.py` and exit 1.

Headless checks use Textual's `run_test` and `save_screenshot`; see the tests in the
scratchpad history for the pattern.

## Building the app

    .venv/bin/pip install -r requirements-build.txt     # PyInstaller
    scripts/build_app.sh                                  # -> dist/agent-farm.app

Steps, all in that script:

1. **PyInstaller onedir** of `main.py` from `packaging/agent-farm-core.spec` into
   `dist/python/agent-farm-core/`. Textual and Rich are collected whole; watchdog is excluded.
2. **`swift build -c release`** in `macos/`. SwiftTerm is linked statically; Sparkle comes
   as a binary framework that SwiftPM downloads into `macos/.build/artifacts`.
3. **Assemble** `dist/agent-farm.app`: the Swift binary, `Sparkle.framework`, the Python
   tree under `Contents/Resources/agent-farm-core`, the icon (drawn by
   `scripts/make_icon.swift` on first build), and `Info.plist` with `VERSION` filled in for
   both `CFBundleShortVersionString` and `CFBundleVersion`.
4. **Sign** with `scripts/sign_app.sh`, inside-out: every Mach-O in the Python tree, the
   nested `Python.framework`, the entry executable with `packaging/python.entitlements`,
   Sparkle's helpers and framework, then the app. Local builds are ad-hoc signed without the
   hardened runtime (an ad-hoc signature has no team, so a hardened-runtime app could not
   load its own frameworks). `CODESIGN_IDENTITY="Developer ID Application: …"` signs for real.

The host app (`macos/Sources/AgentFarm/AppDelegate.swift`) sets `COLORTERM=truecolor`,
a UTF-8 locale and a PATH that includes Homebrew and iTerm2's `it2` for the child, maps
`q` to quitting the app, shows a restart hint if the Python side exits with an error, and
provides the menus. `swift run` from `macos/` works without a bundle for UI work; set
`AGENT_FARM_CORE=/path/to/executable` to choose what it runs, and Sparkle stays off because
there is no `Info.plist`.

Builds are Apple-silicon only, like the machine they are built on.

## Releasing

Each release is a tag `v<VERSION>` and a GitHub release carrying `agent-farm-<VERSION>.zip`
and `appcast.xml`. Installed apps poll
`https://github.com/djp3/agent-farm/releases/latest/download/appcast.xml`, which GitHub
redirects to the newest non-prerelease release, so creating the release is what ships the
update. Sparkle compares `CFBundleVersion`, which is `VERSION`, so bump it every release.

### One-time setup

- A **Developer ID Application** certificate in the login keychain. `release.sh` picks the
  first one, or set `CODESIGN_IDENTITY`.
- **Notarization credentials** as a notarytool keychain profile named `agent-farm`:

      xcrun notarytool store-credentials agent-farm --apple-id <apple id> --team-id <team id> --password <app-specific password>

- The **Sparkle signing key**. The public half is in `macos/Info.plist` (`SUPublicEDKey`)
  and the private half lives in the login keychain under the account `agent-farm`:

      scripts/fetch_sparkle_tools.sh                       # downloads generate_keys, generate_appcast, sign_update
      .sparkle-tools/<ver>/bin/generate_keys --account agent-farm        # prints the public key already in Info.plist

  A new key pair means a new public key in `Info.plist`, and apps already installed will
  reject updates signed with it, so keep this one backed up:
  `generate_keys --account agent-farm -x sparkle_ed25519` exports it.
- `gh` logged in with push access.

### Each release

    echo 0.2.0 > VERSION
    git commit -am "Release 0.2.0" && git push
    scripts/release.sh

The script refuses to run with uncommitted changes or an existing tag, builds with the
Developer ID, notarizes with `notarytool --wait`, staples, verifies with `spctl`, zips with
`ditto`, writes and signs the appcast with `generate_appcast`, tags, pushes the tag and
creates the release with generated notes. Delete a bad release on GitHub and `latest`
falls back to the previous one.

### From GitHub Actions

`.github/workflows/release.yml` runs the same script on a macOS runner; start it from the
Actions tab after `VERSION` is bumped on `main`. It needs these repository secrets:

| secret | value |
|--------|-------|
| `DEVELOPER_ID_P12` | base64 of a `.p12` export of the Developer ID Application certificate and key |
| `DEVELOPER_ID_P12_PASSWORD` | its password |
| `NOTARY_KEY_ID`, `NOTARY_ISSUER_ID`, `NOTARY_KEY_P8` | an App Store Connect API key with the Developer role: id, issuer id, and the `.p8` contents |
| `SPARKLE_PRIVATE_KEY` | the exported Sparkle private key (`generate_keys --account agent-farm -x -`) |

Running the workflow and the local script for the same version is not possible: whichever
runs second stops at the existing tag.
