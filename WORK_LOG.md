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

## Version 1.0.3 — skins without a persisted settings file

Kodi permits an active skin to have no user `settings.xml`; this represents an empty/default setting set. Backup previously treated that valid state as an error and skipped AF3 helper data. It now records a validated empty settings document while continuing to collect Skin Variables menus, widgets, views and profiles. This also gives restore an explicit representation of the default state, while reset detection still notices a later drop from a populated settings file.

All 37 automated tests pass. Kodi 21.1's embedded Python passed all eight native checks, including collection from a temporary skin profile with no `settings.xml`; the live AF3 read-only snapshot also passed.

## Version 1.0.4 — native persistence guard

The idle service checks the active skin's `settings.xml` every five minutes and asks Kodi's native `Skin.SetString` path to recreate it when missing or invalid. Every manual or scheduled backup forces the same native save, waits beyond Kodi's deferred-save interval, validates the XML, and only then reads the snapshot. If Kodi still cannot persist it, the version 1.0.3 empty/default fallback preserves helper data and a manual backup reports the condition.

The guard uses a fixed private marker so repeated saves do not create spurious backup changes. It never writes the live skin settings file directly. All 40 automated tests pass, including native-save invocation, graceful fallback, and no unnecessary background rewrite of an existing valid file. Kodi 21.1 wrote the marker into the live AF3 settings file through its native saver and passed the full device-local self-test.

## Version 1.0.5 — cross-device live restore

A seven-file cross-device archive confirmed that collection succeeded, narrowing the observed failure to activation. Restore previously replaced the verified files and assumed that switching back to the target skin would discard the destination device's cached values. Version 1.0.5 stores the decoded source values in the durable pending transaction, creates any source-only setting IDs through safe Kodi built-ins, resets destination-only values, applies every archived bool/string through `Settings.SetSkinSettingValue`, and invokes Kodi's native saver. It then reloads the skin from the persisted document and verifies every live value before AF3 reads the restored Skin Variables JSON and rebuilds its categories and widgets.

The actual Family Room archive showed why version 1.0.5 could truthfully verify a no-op: its seven files included six populated Skin Variables JSON files but an empty `settings.xml` with zero settings. Version 1.0.6 builds the settings document from Kodi's live JSON-RPC map instead of trusting the stale tvOS file, and it includes inferred Skin Variables profile node directories even when `skinusers.json` is absent. Restore now displays the archived setting/helper counts, warns on zero-setting legacy archives, and verifies both the live values and helper-file hashes after AF3's final rebuild and reload.

The Family Room 1.0.6 backup contained 264 settings even though Kodi's delayed native saver still did not create its profile document. On Bonus Room, all 264 values were present in both the pending transaction and restored `settings.xml`, but the subsequent live replay reset AF3 and failed on `Settings.SetSkinSettingValue`, leaving the skin blank. Version 1.0.7 removes that destructive reset/replay sequence: the Estuary-to-AF3 activation loads the complete restored document and the add-on verifies the live map before AF3 rebuild. It deliberately avoids `ReloadSkin`, which saves the current live state before loading and can overwrite the imported file. The persistence guard now creates the profile directory, waits up to five seconds for Kodi's saver to produce a semantically identical document, and atomically writes and verifies the live map as a recovery file when Kodi still does not persist it.

All 46 automated tests pass. Kodi 21.1's embedded Python passed the native self-test, including creation and verification of a real archive containing all 280 live AF3 settings.

The background persistence check now avoids pending restores and uses the operation lock only when a missing or invalid file needs repair. Restore can no longer report completion when Kodi retained different live skin settings. All 43 automated tests pass, including typed archive decoding, a simulated Family-to-Bonus live-state replacement, and applying live settings before AF3 rebuilds.

## Version 1.0.8 — restore and persistence hardening

The five-minute persistence guard now compares the valid on-disk document with Kodi's complete live typed setting map, repairing stale XML as well as missing or malformed XML. Its fallback follows live changes during Kodi's delayed-save window and writes only after several identical reads; a continuously changing skin is left untouched for a later retry.

Restore recovery now preserves ambiguous pending state, ignores transaction journals belonging to other Kodi profiles, and validates JSON objects before using them. A staged, completed restore can be explicitly cancelled from another active skin; the add-on restores the exact pre-restore transaction snapshot, rebuilds from those previous files, and verifies them before clearing pending state. Menus show only the completion or recovery actions relevant to the current phase, and staged files are no longer described as a completed restore.

AF3 rebuild output is parsed before its required synchronous skin reload. Restored live settings are verified immediately before the reload and again afterward, along with helper file hashes. The generated skin-user include uses the same durable atomic writer as the persistence guard. Skin switching now tolerates slow loading while requiring the selected skin to remain active beyond Kodi's confirmation window.

All 61 automated tests pass, along with Python compilation, whitespace checks, deterministic ZIP CRC verification and SHA-256 verification. Version 1.0.8 passed all eleven native diagnostics on Bonus Room with Kodi 21.3 and Python 3.11.7, including a completed-transaction rollback in an isolated temporary profile. During the first installation attempt, Kodi started with its cache-based profile unavailable, failed to load `guisettings.xml` from tvOS `NSUserDefaults`, and created a fresh Estuary profile before the add-on service ran. The device was recovered from its September 3 full backup and September 6 skin-only snapshot. After the final diagnostic run and restart, Kodi loaded AF3 3.2.19 with 269 persisted settings; all six category, widget, and viewtype helper files matched the snapshot byte for byte, and the normal background service was restored byte for byte.

## Version 1.0.13 — tvOS VFS restore recovery and visible progress

Version 1.0.13 addresses a tvOS restore where all 264 imported settings were intact in the pending transaction and on disk but Kodi reactivated AF3 with a stale live setting map. Restore now stages `settings.xml` through Kodi's VFS while the target skin is inactive and verifies the VFS read-back. When the user explicitly chooses **Finish restored skin**, an existing pending restore can recover by switching to Estuary, restaging the same verified document, and reactivating AF3; the background service never initiates those skin switches. No global skin reset or per-setting replay is used. Failures preserve the transaction, and a failed VFS write restores the prior VFS document when possible.

Modal progress is shown throughout ordinary work, with a notification and nonblocking indicator during Kodi's skin-confirmation window. Instructional activation prompts use one OK button, the menu calls its documentation item Instructions / Readme, and the executable extension is listed first for Kodi's Run action. All 72 automated tests pass, along with Python compilation and whitespace checks.

## Version 1.0.14 — confirmation-aware progress

Skin switching now watches Kodi's actual DialogConfirm visibility. The nonblocking indicator remains in place while **Keep this skin** owns focus, then modal progress resumes as soon as that dialog closes. This removes the unexplained pause after confirmation and prevents the progress window from opening over Kodi's Yes/No buttons and causing an unintended reversion to Estuary.

## Version 1.0.15 — resilient recovery and clearer workflows

Restore now records its transaction before copying rollback data, so a crash during snapshot creation is recognizable and safely recoverable without changing target files. Recovery also recognizes an automatic rollback that finished before pending metadata was cleared, restages and verifies the recovered settings through Kodi VFS, and releases the pending marker. Unreadable pending metadata can be archived byte-for-byte and cleared without touching Kodi, skin, helper, or add-on configuration files. Symlinked rollback journals are rejected.

Pending restores now show resolution actions first and hide unrelated backup/restore commands. Menu and setting labels use direct terms such as **Back up current skin**, **Choose backup folder**, **Complete pending restore**, and **Keep current files and clear restore status**. Confirmation instructions consistently say to choose **Yes** when Kodi asks whether to keep a skin. The settings category was shortened to **General** for narrow skin layouts. Restore confirmation describes exactly which settings and helper files will be replaced and identifies skin-version differences.

Modal progress cancellation is honored while preserving completed backups and pending recovery data. Skin-confirmation progress remains nonblocking until Kodi's Yes/No dialog closes, and a reverted skin activation fails rather than being treated as success. Command-line system checks now display their result, and package verification uses explicit checks that remain active under optimized Python.

All 82 automated tests pass, including Skin Variables build-hash changes, simulated crashes during rollback snapshot creation, already-completed automatic rollback recovery, malformed pending-state escape, symlink rejection, cancellation, delayed skin confirmation, skin reversion, and clean/pending menu states. Python compilation and Git whitespace checks pass.

Native validation used the exact 1.0.15 ZIP on Kodi 21.1 with Python 3.11.7. The system check passed 11 of 11 checks; manual backup saved 276 user skin settings and eight managed files; restore staged through Estuary with visible progress, retained a pending transaction across rejected skin confirmations, rebuilt AF3 with visible progress, verified all restored settings and seven helper files, and cleared the pending marker. The test discovered and fixed four Skin Variables build fingerprints that must change during regeneration and are now excluded from user-setting backup and verification.

## Version 1.0.16 — streamlined menu, expanded help and new icon

The main menu now uses **Backup current skin**, **Settings**, and **Help and Status**. The duplicate **Choose backup folder** command and its unused runtime branch were removed; Kodi's native folder picker remains available through **Settings → Backup folder**. The Help and Status screen explains each primary backup, restore, import, and system-check action. The optional device-name setting now explains that its value identifies the source device in backup details without changing storage or Kodi identity.

The add-on thumbnail now uses a high-contrast painter's palette, brush, and circular backup arrow designed to remain recognizable in Kodi's compact add-on views. All 83 automated tests pass, together with Python compilation and Git whitespace checks.
