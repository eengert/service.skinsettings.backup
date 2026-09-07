# Skin Settings Backup

A small Kodi program and background service for versioned skin-setting backups. Python standard library only; no database scans, thumbnail copying, artwork downloads or cloud SDKs.

## Install and use

1. In Kodi, enable installation from unknown sources if needed, then **Add-ons → Install from zip file**, selecting `service.skinsettings.backup-1.0.13.zip`.
2. Open **Program add-ons → Skin Settings Backup → Settings → Backup destination**. Select a writable folder in Kodi’s folder picker, preferably on a Mac or NAS. Network sources added in Kodi’s File Manager are available in the picker. The add-on menu’s **Choose destination** action also opens a folder picker. Configure share authentication through Kodi's network sources instead of embedding passwords in the path.
3. Select **Back up now** to create and verify your initial protected snapshot. Select **Save protected snapshot** before a skin update or after a major customization.
4. In Settings, select an interval (1, 6, 12, 24 or 168 hours) and how many automatic backups to keep (default 14). Scheduling is enabled once a destination is set. The first snapshot and explicitly protected snapshots never expire.

The service waits 45 seconds after startup and works only while Kodi is idle and no media is playing. It catches up after a missed interval when Kodi runs again. It cannot wake a suspended Apple TV or run while Kodi is closed. Unchanged settings do not create additional automatic archives. Failed operations retry after five minutes; repeated identical failures notify at most once a day. Manual backups may run while Kodi is playing; restores require stopped playback.

Each device/profile/skin has its own folder under `SkinSettingsBackup`. The device identifier is generated at first run. If you clone an entire Kodi userdata folder to another device, remove **only this add-on's** `identity.json` and `state.json` on the new device before using it so the clone gets a separate backup identity.

## Settings labels and upgrades

If settings controls appear without labels after a manually copied development installation, fully quit and reopen Kodi. A directory scan alone does not load the add-on language catalog. Normal Kodi installation and updates load it automatically. The built-in self-test now checks every settings label, help text and interval choice.

## Coverage

| Configuration | Included |
| --- | --- |
| Any skin's `addon_data/<skin-id>/settings.xml` | Yes |
| Skin Variables menus and widgets for that skin | Yes |
| Skin Variables view-type preferences | Yes |
| Skin Variables skin-user declarations and their menus/widgets | Yes |
| AF3 generated menu/view XML | Regenerated after restore using installed Skin Variables |
| Other helpers such as Skin Shortcuts or Skin Helper | Not in version 1 |
| Kodi font/theme/color/skin-zoom selections | Yes, when exposed by Kodi |
| Kodi view database and other global settings | Not included |
| Custom image/font files, playlists, media sources, libraries, other add-on accounts | Not included |

Kodi does not always keep the on-disk skin `settings.xml` synchronized with its live settings, especially on tvOS. Backups therefore capture the active skin's authoritative live bool/string setting map through Kodi's JSON-RPC API and encode it as a portable `settings.xml`. The file on disk remains a persistence safeguard rather than the backup source. Skin Variables profile folders are discovered from both its user declaration and its existing profile node directories.

The service compares the active skin's `settings.xml` with Kodi's live setting map every five minutes while Kodi is idle. It asks Kodi's native skin-setting saver to repair a missing, invalid or stale document. If Kodi's delayed saver still does not finish, the add-on waits until the live values are stable, atomically writes the latest complete map, and verifies both the file and live values. Every backup repeats this guard, and the archive is built directly from the live map so a missing or stale file cannot silently produce an empty backup. A manual backup reports exactly how many skin settings and helper files were saved and whether it repaired the persistence file.

AF3 is the first skin with a dedicated rebuild adapter. Generic support restores the main settings XML; a different skin's separately stored helper configuration may need another adapter. Linked artwork, playlists and widget-provider add-ons must still be available at their original paths. A backup is not a full portable Kodi installation.

## Restore

Open **Restore backup** to choose a restore point from this device/profile, or **Import backup ZIP** to select an archive from another device. Import reads the skin ID from the verified archive. Install that skin and its dependencies first. The confirmation shows the skin, device, date, saved skin-setting count, helper-file count and version mismatch, if any. It warns when an older archive contains zero skin settings.

When restoring the active skin, the add-on switches to Estuary (or Estouchy when restoring Estuary). **Accept Kodi's “Keep this skin” prompt.** The add-on waits for the confirmation timeout to pass before replacing files. If neither fallback is installed, manually switch to another skin and run Restore again.

Before writing settings, the add-on saves a durable local rollback copy. It validates paths, checksums, XML and JSON, replaces only the managed files, and removes stale managed JSON files. It attempts rollback if writing fails. After restoring, accept activation of the restored skin and Kodi's skin-change prompt. AF3 then rebuilds its generated views and menus. This rebuild and Kodi's skin switches take longer than copying the tiny backup archive.

While the target skin is inactive, the add-on stages its complete verified `settings.xml` through Kodi's VFS as well as the transactional local restore. This matters on tvOS, where Kodi's native file store can otherwise ignore a correct file written through Python's ordinary filesystem APIs. After activation, the add-on verifies every live value before rebuilding AF3. If Kodi still loads a stale cached copy, **Finish restored skin** automatically switches to Estuary, restages the same pending document through Kodi, and reactivates the target skin without resetting or replaying individual settings. The pending transaction remains available if any step fails.

Interactive backups, restores, recovery, helper generation and final verification use a modal progress dialog. During Kodi's own **Keep this skin** confirmation window, a notification and nonblocking progress indicator remain visible; the modal progress dialog resumes as soon as the skin change is confirmed.

If you choose to activate the skin later, select **Finish restored skin** after activating it; the service also finishes when that skin becomes active and idle. Before finishing, **Restore previous settings** can cancel the staged restore from a different active skin and put back the exact local transaction snapshot. Activate that skin and select **Finish restoring previous settings** so its menus are rebuilt and the previous files are verified before pending state is cleared. If Kodi closed during restoration, use **Recover interrupted restore** from a different skin. Completed writes interrupted just before rebuilding are resumed; partially applied writes are rolled back. If transaction evidence is missing or ambiguous, pending state is preserved and backups remain blocked instead of assuming recovery succeeded. Scheduling pauses while a restore remains unfinished. Local transaction copies remain in this add-on's `rollback` folder for inspection; they are not automatically expired in version 1.

## Integrity and protection

Archives contain `manifest.json` and a narrowly scoped `files/` tree. Every file has a SHA-256 checksum. A backup becomes visible only after its uploaded ZIP has been read back and a completion record written and verified. Retention runs only after successful publication. A protected flag is checked inside an archive before automatic deletion, so a damaged listing record cannot unprotect it.

The add-on rejects suspicious drops in settings, configured values or helper-file counts during automatic backup. A manual confirmation can save the changed configuration as a separate protected snapshot. This is a heuristic, not a guarantee that every reset can be detected. Multiple versions and the permanently retained initial snapshot provide additional protection.

Backups are not encrypted. Skin settings may include private widget URLs or skin-profile PINs. Choose a destination you control. Network destinations need write access; read-only shares will fail verification. Interrupted uploads without a valid completion record are ignored and can be removed manually from this add-on's destination folder.

Limits: 2,000 files, 10 MiB per file, 50 MiB uncompressed total, 55 MiB archive. Settings for a typical AF3 setup are much smaller. Symlinks and archive paths outside the allowed scope are rejected. Directory fsync is used on Unix; Windows uses flushed file writes and atomic replacement without directory fsync.

## Verification and development

**Run self-test** exercises Kodi's actual VFS, archive verification and restore against a temporary fixture profile, then performs a read-only backup of the current skin. It does not restore or change your current skin settings. Its report is written to this add-on's `self-test-report.json`.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/package.py
```

Testing on an Apple TV should include: a self-test, backup to the intended network destination, an intentional visible setting/widget change, restore of the protected snapshot, and a restart to verify persistence. This device-level restore check cannot be replaced by desktop or simulated tests.

Source references: [Kodi skin loading](https://github.com/xbmc/xbmc/blob/Omega/xbmc/application/ApplicationSkinHandling.cpp), [Kodi skin setting serialization](https://github.com/xbmc/xbmc/blob/Omega/xbmc/addons/Skin.cpp), [Skin Variables](https://github.com/jurialmunkey/script.skinvariables).
