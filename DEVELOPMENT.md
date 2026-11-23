# Developing agent-farm

The monitor is a Python [Textual](https://textual.textualize.io) program. The Mac app is a
thin native shell around it: a window with a [SwiftTerm](https://github.com/migueldeicaza/SwiftTerm)
terminal that runs a PyInstaller build of the Python code, plus
[Sparkle](https://sparkle-project.org) for updates. Everything the monitor does happens in
Python, so day-to-day development never touches Swift or the bundle.

## Layout

| path | what |
|------|------|
| `main.py` | the Textual app: tree, detail pane, expiring pane, fleet line, usage panel, settings screen |
| `collector.py` | the passive data layer: registry, transcripts, subagents, the shells under each session, git, and the iTerm2 tab map over one Python API connection that follows iTerm2's layout announcements (`it2` only as a focus fallback); `python collector.py` prints a one-shot dump |
| `usage.py` | incremental transcript scanner behind the usage panel: daily tokens and cost, and when each agent was running over the last day, background commands included |
| `projects.py` | which project a folder belongs to: its git repo (worktrees merged, clones merged by origin), else the folder its session started in |
| `expiring.py` | the sessions Claude Code's 30-day cleanup deletes next (by file modification time, as Claude Code judges them), and keep: the app's one write, which sets those times to now |
| `limits.py` | plan limits (the `/usage` windows) from Anthropic's OAuth usage endpoint, with the Claude Code sign-in from the Keychain; reads both reply shapes (the `limits` rows and the older keys); opt-in, via system `curl` |
| `ordering.py` | the orderings; an `Ordering` is a tuple of `Tier`s, each with a `match` predicate and a sort `key`; add one and append it to `ORDERINGS` |
| `settings.py` | persisted settings; a new setting is a dataclass field with a default |
| `grange.py` | reporting to agent-grange: which sessions go (the patterns from the settings, `reported(name, patterns)`, and `samples(pattern)`, which draws strings a pattern matches from Python's own parse of it), `report()` for the entries, `Reporter` for when a push is due, `History` for when reporting was on and how the grange answered (the band under agents at work), and `put_farm()`, the `PUT /v1/farm` to the grange on this Mac. The contract is agent-grange's `docs/requirements.md`, sections 4.1 and 5.1 |
| `apppaths.py` | where logs and settings live: next to the code from a checkout, under `~/Library` in the app |
| `supervisor.py`, `supervisor.sh` | development runner that restarts `main.py` when a `.py` file changes |
| `macos/` | the SwiftPM package for the host app (`Package.swift`, `Sources/AgentFarm`, `Info.plist`) |
| `packaging/` | the PyInstaller spec and the hardened-runtime entitlements for the Python side |
| `scripts/` | `build_app.sh`, `sign_app.sh`, `release.sh`, `sparkle_tools.sh`, `make_icon.swift` |
| `.github/workflows/release.yml` | the same release, run on GitHub's macOS runner |
| `VERSION` | the single source of the version number |

## Running from source

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    ./supervisor.sh                  # runs main.py and hot-reloads it on any .py change
    .venv/bin/python -m pytest       # the test suite (pytest is not in the requirements)
    .venv/bin/python main.py         # plain run
    .venv/bin/python main.py --usage --order activity   # one-run overrides of the saved settings

From a checkout the settings file is `settings.json` next to the code (or
`AGENT_FARM_SETTINGS`), and logs go to `logs/agent-farm.log` and `logs/supervisor.log`.
`AGENT_FARM_ORDER` and `AGENT_FARM_USAGE=1` do what the flags do. The pre-rename names
(`CLAUDE_MONITOR_*`, `monitor_settings.json`) are still read for one release; an old
settings file is renamed in place on first load.

The supervisor ignores `.venv`, `build`, `dist`, `.build` and `.sparkle-tools`, so building
the app does not restart the monitor you are looking at. It restarts the child only; edits
to `supervisor.py` itself need a manual restart. The child's stderr is deliberately not
captured, because Textual draws on stderr; crashes are logged by `main.py` and exit 1.

The tests in `tests/` are plain pytest; the ones that drive the UI use Textual's
`run_test` with a pinned snapshot (see `tests/test_focus_async.py` for the pattern).

## Building the app

    python3.14 -m venv .venv                              # once, if "Running from source" did not
    .venv/bin/pip install -r requirements.txt -r requirements-build.txt   # the app's libraries, and PyInstaller and pip-tools
    scripts/build_app.sh                                  # -> dist/agent-farm.app

Both files go into the same `.venv`: PyInstaller packs the app's libraries from the Python it
runs in. Left to itself it only warns about one it cannot find, and builds an app that fails
as it opens (`No module named 'rich'`), so the build checks for them first and stops,
naming what is missing and the `pip install` line that installs it.

Dependencies are pinned exactly in `requirements.txt` and `requirements-build.txt`, and
`requirements.lock` lists every package, indirect ones included, with the hashes of their
files. The release workflow installs from the lock with `--require-hashes`, so what ships
is decided by the repository, not by the package index on release day. To upgrade a
library: bump its pin, regenerate the lock, run the tests, commit all three together. The
command below keeps every indirect package at the version already locked; add `--upgrade`
to move them all to their latest versions too:

    .venv/bin/pip-compile --generate-hashes --strip-extras --allow-unsafe \
        -o requirements.lock requirements.txt requirements-build.txt

Steps, all in that script:

1. **Check the libraries** with `scripts/check_requirements.py`: every pin in
   `requirements.txt` and `requirements-build.txt` must be installed in `.venv` at its
   version, and every line of both must be an exact pin (`name==version`), so none goes
   unchecked. Without a `.venv` at all the build says how to make one.
2. **PyInstaller onedir** of `main.py` from `packaging/agent-farm-core.spec` into
   `dist/python/agent-farm-core/`. Textual and Rich are collected whole; watchdog is excluded.
3. **Start the core once**, with `--help`, which loads everything `main.py` imports and
   exits before the monitor begins (no log, no settings, nothing written). The build stops,
   showing what the core said, if it cannot start; a core reused with `SKIP_PYTHON=1` is
   started too.
4. **`swift build -c release`** in `macos/`. SwiftTerm is linked statically; Sparkle comes
   as a binary framework that SwiftPM downloads into `macos/.build/artifacts`.
5. **Assemble** `dist/agent-farm.app`: the Swift binary, `Sparkle.framework`, the Python
   tree under `Contents/Resources/agent-farm-core`, the icon (Noto Emoji's 3D rooster from
   `macos/Resources/rooster.png`, drawn by `scripts/make_icon.swift` whenever the script or
   the image is newer than the cached `AppIcon.icns`; never Apple's emoji artwork, which is
   not licensed for redistribution), its license notice, the licenses of Sparkle and SwiftTerm
   (from the packages SwiftPM fetched, as `LICENSE-Sparkle` and `LICENSE-SwiftTerm` beside
   the app's own; the build stops without them), and `Info.plist` with `VERSION` filled in for both
   `CFBundleShortVersionString` and `CFBundleVersion`.
6. **Check the minimum macOS** with `scripts/check_min_macos.py`. It reads each binary's
   Mach-O header itself (no otool, standard library only) and counts only the Apple-silicon
   slices. The build stops if:
   - any binary needs a newer macOS than `LSMinimumSystemVersion` declares;
   - a binary records no minimum at all;
   - the Swift executable is missing, or records a different minimum (`Package.swift` and
     `Info.plist` must agree).
   
   agent-frolic runs the same check.
7. **Sign** with `scripts/sign_app.sh`, inside-out: every Mach-O in the Python tree, the
   nested `Python.framework`, the entry executable with `packaging/python.entitlements`,
   Sparkle's helpers the way Sparkle's own instructions re-sign them (only `Downloader.xpc`
   keeps the entitlements it ships with; never `--deep`), its framework, then the app. The
   build signs with the first Developer ID Application certificate in the keychain, so a
   local build keeps the privacy grants a release has, or `CODESIGN_IDENTITY`. Without one it
   signs ad-hoc and without the hardened runtime (an ad-hoc signature has no team, so a
   hardened-runtime app could not load its own frameworks).

Only a release looks for updates. A copy built by `scripts/build_app.sh` alone has no
`SUFeedURL` (nor the automatic-check keys), so it starts no updater and **Check for Updates…**
stays grey: it has the installed app's bundle id, and so its Sparkle preferences, and with
automatic installs on it would be replaced by the published app as it quit. `release.sh`
builds with `WITH_UPDATES=1`, which keeps the feed. The build also holds SwiftPM to
`Package.resolved` (`--only-use-versions-from-resolved-file`), keeps only the
`@executable_path/../Frameworks` rpath and stops if that is not what is left.

The host app (`macos/Sources/AgentFarm/AppDelegate.swift`) sets `COLORTERM=truecolor`
and a UTF-8 locale for the child (helpers are run by absolute path, so no PATH changes),
quits when the Python side exits cleanly (after `q` in the monitor), shows a restart hint
if it exits with an error, and provides the menus. Closing its window quits it, whatever else
is open (Sparkle's update window included). `swift run` from `macos/` works without a bundle
for UI work; set `AGENT_FARM_CORE=/path/to/executable` to choose what it runs (only a run
without a bundle id honours it), and Sparkle stays off because there is no `Info.plist`.

An app built for macOS 27 gets its window title at the left of the title bar, and AppKit has
no setting for that. `CenteredTitle.swift` hides the system's title and draws it in the
middle with a label of its own. The window keeps its real title, which the Dock, Mission
Control, the Window menu and VoiceOver use, and the label follows every change to it. The
same class is in agent-frolic.

Builds are Apple-silicon only, like the machine they are built on.

**The minimum macOS is 27**, declared in `macos/Info.plist` (`LSMinimumSystemVersion`, which
Finder checks and the update feed passes to Sparkle) and in `macos/Package.swift`. It
follows the bundled Python. Releases are built with Homebrew's Python, and Homebrew builds
it for the macOS it runs on, so Python itself, its standard-library modules and the
Homebrew libraries they load (OpenSSL, xz and others) record macOS 27 as their minimum. The
rest records older versions: the wheels from PyPI, PyInstaller's launcher, Sparkle, and
Homebrew libraries that were last built on an older macOS. When the build Mac moves to a
newer macOS and Homebrew's packages follow, step 4 stops the build and lists the files. Then either raise the minimum in both files and the
README, or build with a Python made for older systems (python.org's installer). The release
workflow on GitHub uses `actions/setup-python`, whose Python targets older macOS versions.

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

      "$(scripts/sparkle_tools.sh)"/bin/generate_keys --account agent-farm -p   # prints the public key already in Info.plist

  `scripts/sparkle_tools.sh` prints the folder of Sparkle's tools (`generate_keys`,
  `generate_appcast`, `sign_update`): the ones SwiftPM fetched with the framework, under
  `macos/.build/artifacts`, each checked against its pinned SHA-256 every time it is asked.
  On a checkout never built it resolves the packages first. If it says to, delete all of
  `macos/.build`: with only its `artifacts` folder gone, SwiftPM believes the download is still
  there. Older releases downloaded the tools to `.sparkle-tools/`, which can be deleted.

  The app takes only an appcast signed with this key (`SURequireSignedFeed`, with
  `SUVerifyUpdateBeforeExtraction`), as well as the zip in it. A new key pair means a new
  public key in `Info.plist`, which installed apps never see: a lost key means nothing can
  be offered to them again. Keep this one backed up:
  `generate_keys --account agent-farm -x sparkle_ed25519` exports it.
- `gh` logged in with push access.

### Each release

    echo 0.2.0 > VERSION
    $EDITOR release-notes/0.2.0.md          # what is new, written for people who use the app
    git add VERSION release-notes && git commit -m "Release 0.2.0" && git push
    scripts/release.sh

The script refuses to run with uncommitted changes, an existing tag or no notes file, and
before it builds anything it checks Sparkle's tools against their pinned hashes and the
Sparkle key against `SUPublicEDKey` (`scripts/check_sparkle_key.sh`, handed the same
`--account agent-farm` or `--ed-key-file <file>` the release signs with; an exported key must
be a regular file, since the release reads it three times). It builds with the Developer ID
and `WITH_UPDATES=1`, then checks that the copy it built carries the update feed, both signed-
update keys and the app's Sparkle key, and checks the tools again. It notarizes with
`notarytool --wait`, staples, verifies with `spctl` and zips with `ditto`. `generate_appcast`
then writes the appcast and signs the zip and the feed; `scripts/check_appcast.py` checks
that both signatures and the notes are there, and `sign_update --verify` that the feed's
signature holds (what it says is shown when it does not). Only then does it tag, push the tag
and create the release. Delete a bad release on GitHub and `latest`
falls back to the previous one. `tests/test_release.py` runs the script with stand-ins for
everything outside it.

The notes in `release-notes/<VERSION>.md` show in two places:

- **The appcast.** Sparkle shows the notes in its update window, rendering them as Markdown.
  The window is small, so keep to a few short paragraphs and `-` lists.
- **The GitHub release.** GitHub's "Full Changelog" link follows the notes.

People who let Sparkle install updates automatically never see the window, so the GitHub
release is the record. The repository is public, so the notes, like the README, use made-up
numbers and names.

### From GitHub Actions

`.github/workflows/release.yml` runs the same script on a macOS runner; start it from the
Actions tab after `VERSION` is bumped on `main`. It needs these repository secrets:

| secret | value |
|--------|-------|
| `DEVELOPER_ID_P12` | base64 of a `.p12` export of the Developer ID Application certificate and key |
| `DEVELOPER_ID_P12_PASSWORD` | its password |
| `NOTARY_KEY_ID`, `NOTARY_ISSUER_ID`, `NOTARY_KEY_P8` | an App Store Connect API key with the Developer role: id, issuer id, and the `.p8` contents |
| `SPARKLE_PRIVATE_KEY` | the exported Sparkle private key (`generate_keys --account agent-farm -x -`) |

Running the workflow and the local script for the same version is not possible: the
preflight refuses when the tag already exists on GitHub. A run that fails while publishing
removes its tag again, so simply re-run it.
