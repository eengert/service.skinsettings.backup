# Delivery record — 2026-09-06

## Success criteria and status

- [x] Separate installable Kodi add-on, with no changes to the existing AF3 source.
- [x] Generic main-skin settings and appearance backup; AF3 Skin Variables menus, widgets, view preferences and skin profiles.
- [x] Selectable writable local/network destination, configurable interval and retention, manual backup and restore.
- [x] Protected initial/manual snapshots; unchanged-backup skipping; persisted pause after suspected resets.
- [x] Verified archives, constrained paths and sizes, transactional restore, rollback and interrupted-write recovery.
- [x] AF3 script-based rebuild integration and resumable restore completion.
- [x] Automated tests, native Kodi smoke tests, documented installation and verified ZIP.
- [ ] Physical Apple TV test against the intended network share, including an actual AF3 restore and restart.

## Decisions and adjustments

The implementation is a standalone service/program rather than a skin modification. Generic support covers standard skin settings; helper-specific adapters extend coverage. AF3 is the first rebuild adapter. Other helpers such as Skin Shortcuts are explicitly outside version 1.

Inspection revealed AF3 skin-user widget directories are siblings of the default widget directory. The archive adapter therefore follows validated skin-user declarations and captures each user's configuration.

Kodi resets appearance selections during skin switches. Theme, colors, font and zoom were added to archive metadata so restoration can reapply them.

Review hardened recovery against crashes between file replacement and rebuild, and against backward clock changes. It also added persistent reset blocking and verified the protected flag inside the ZIP before retention deletes it.

The native smoke test was corrected to use explicit checks rather than Python assertions: Kodi can disable assertions. The final smoke test actually executes native backup orchestration twice and verifies deduplication.

Network reads request the actual file length, avoiding a maximum-size buffer allocation for tiny archives on older devices. No third-party Python dependencies were added.

## Verification

- `python3 -m unittest discover -s tests -q`: **36 tests passed**.
- Python compilation and Git whitespace checks passed.
- Local Kodi **21.1**, embedded Python **3.11.7**: all six native smoke checks passed.
- AF3 snapshot: **8 files, 28,048 source bytes, 8,263 archive bytes**; collection, compression and verification took **0.0224 seconds** on this Mac.
- Full smoke test including two native backup stability waits: **4.1137 seconds**.
- Every installed add-on file matched the packaged ZIP. ZIP CRC checks and packaged XML parsing passed.
- Native restore writes were confined to a temporary fixture profile. Existing live skin settings were only read.

The native report is saved in `reports/kodi-self-test.json`. The ZIP is built reproducibly by `scripts/package.py`; tests, reports, Git data, bytecode and macOS metadata are excluded.

## Delivery and remaining validation

Version **1.0.0** is packaged in `dist/service.skinsettings.backup-1.0.0.zip` with a SHA-256 sidecar. The same version is installed and enabled in local Kodi. No backup destination has been selected, so scheduled backups will not run until configuration is completed. Nothing was published to a remote repository.

Apple TV validation could not be performed from this environment: no usable device-control tool was available, and the intended backup share has not been selected. Install the ZIP on an Apple TV, choose the share, run the built-in self-test, save a protected snapshot, change a visible setting/widget, restore, and restart Kodi to verify persistence. Actual AF3 skin-switch/rebuild behavior, tvOS filesystem behavior and network-share permissions remain subject to that test.

## Version 1.0.1 — settings labels and thumbnail

The local development installation was registered using UpdateLocalAddons, which scans add-ons but does not load language catalogs. The original English PO was valid. A normal Kodi startup loaded all 15 missing strings; the settings dialog was then captured and visually verified in AF3. Normal install/update also loads the catalog through Kodi’s installer.

The native self-test now checks all settings labels, help text and interval options, in addition to values and backup/restore functionality. All seven native checks pass and the 36 automated tests pass. The language-loading failure was reproduced before the restart.

Added an original AI-generated 512-pixel thumbnail, declared it in metadata, and taught packaging to include declared assets. Runtime settings and backup behavior are unchanged. Updated installation troubleshooting in README.

## Version 1.0.2 — destination folder picker

Changed Backup destination from a text editor to Kodi's native writable path picker, including File Manager sources. The setting ID is unchanged and path values remain strings, preserving existing destinations. Updated help and installation instructions.

All 36 automated tests pass. Installed 1.0.2 in idle local Kodi and visually verified that selecting Backup destination opens the native folder picker; cancelled without changing the destination. Repository index, checksums and packaged content were checked, with other add-ons unchanged.
