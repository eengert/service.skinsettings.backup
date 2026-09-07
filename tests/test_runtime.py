import datetime
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SKIN_ID = "skin.example"


class _KodiEnvironment:
    def __init__(self):
        self.profile = None
        self.addon_data = None
        self.skin = SKIN_ID
        self.settings = {}
        self.fail_record_write = False


ENV = _KodiEnvironment()


class _Monitor:
    def waitForAbort(self, _seconds):
        return False

    def abortRequested(self):
        return False


class _Player:
    def isPlaying(self):
        return False


class _Dialog:
    notifications = []

    def yesno(self, _title, _message):
        return True

    def ok(self, _title, _message):
        return None

    def notification(self, *_args):
        self.notifications.append(_args)
        return None


class _DialogProgress:
    instances = []

    def __init__(self):
        self.events = []
        self.__class__.instances.append(self)

    def create(self, heading, message=""):
        self.events.append(("create", heading, message))

    def update(self, percent, message=""):
        self.events.append(("update", percent, message))

    def close(self):
        self.events.append(("close",))


class _DialogProgressBG(_DialogProgress):
    def update(self, percent, heading="", message=""):
        self.events.append(("update", percent, heading, message))


class _Window:
    properties = {}

    def __init__(self, _window_id):
        pass

    def clearProperty(self, key):
        self.properties.pop(key, None)

    def setProperty(self, key, value):
        self.properties[key] = value

    def getProperty(self, key):
        return self.properties.get(key, "")


class _Addon:
    def __init__(self, addon_id):
        self.addon_id = addon_id

    def getAddonInfo(self, key):
        if key == "profile":
            return "special://profile/addon_data/service.skinsettings.backup"
        if key == "version":
            return "3.0.0"
        if key == "path":
            return str(ENV.addon_data / self.addon_id)
        return ""

    def getSetting(self, key):
        return str(ENV.settings.get(key, ""))

    def getSettingInt(self, key):
        return int(ENV.settings.get(key, 0))

    def getSettingBool(self, key):
        return bool(ENV.settings.get(key, False))


class _VFSFile:
    def __init__(self, path, mode=None):
        self.path = Path(_translate_path(path))
        self.mode = mode
        self.handle = None
        if mode == "w":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not (ENV.fail_record_write and str(self.path).endswith(".zip.json")):
                self.handle = self.path.open("wb")

    def size(self):
        return self.path.stat().st_size

    def readBytes(self, amount):
        with self.path.open("rb") as handle:
            return handle.read(amount)

    def write(self, data):
        if self.handle is None:
            return False
        self.handle.write(bytes(data))
        self.handle.flush()
        return True

    def close(self):
        if self.handle is not None:
            self.handle.close()


def _translate_path(path):
    prefix = "special://profile/"
    if path.startswith(prefix):
        return str(ENV.profile / path[len(prefix):])
    return path


xbmc = types.ModuleType("xbmc")
xbmc.LOGWARNING = 2
xbmc.Monitor = _Monitor
xbmc.Player = _Player
xbmc.getSkinDir = lambda: ENV.skin
xbmc.getInfoLabel = lambda _label: "Test Kodi"
xbmc.getCondVisibility = lambda _condition: True
xbmc.executebuiltin = lambda _command, _wait=False: None
xbmc.executeJSONRPC = lambda _request: json.dumps({"jsonrpc": "2.0", "id": 1, "result": True})
xbmc.log = lambda *_args: None

xbmcgui = types.ModuleType("xbmcgui")
xbmcgui.NOTIFICATION_INFO = "info"
xbmcgui.NOTIFICATION_WARNING = "warning"
xbmcgui.Dialog = _Dialog
xbmcgui.DialogProgress = _DialogProgress
xbmcgui.DialogProgressBG = _DialogProgressBG
xbmcgui.Window = _Window

xbmcaddon = types.ModuleType("xbmcaddon")
xbmcaddon.Addon = _Addon

xbmcvfs = types.ModuleType("xbmcvfs")
xbmcvfs.translatePath = _translate_path
xbmcvfs.File = _VFSFile
xbmcvfs.exists = lambda path: Path(_translate_path(path)).exists()
xbmcvfs.mkdirs = lambda path: (Path(_translate_path(path)).mkdir(parents=True, exist_ok=True) or True)
xbmcvfs.listdir = lambda path: (
    [item.name for item in Path(path).iterdir() if item.is_dir()],
    [item.name for item in Path(path).iterdir() if item.is_file()],
)
xbmcvfs.copy = lambda source, target: (
    Path(target).parent.mkdir(parents=True, exist_ok=True) or shutil.copyfile(source, target) or True
)
xbmcvfs.delete = lambda path: (Path(path).unlink() or True) if Path(path).exists() else False

# Kodi modules must exist before importing the runtime orchestration module.
sys.modules.update({"xbmc": xbmc, "xbmcgui": xbmcgui, "xbmcaddon": xbmcaddon, "xbmcvfs": xbmcvfs})
sys.path.insert(0, str(ROOT))
from resources.lib import runtime  # noqa: E402


def _settings(count, marker):
    values = "".join('<setting id="item{}">{}-{}</setting>'.format(i, marker, i) for i in range(count))
    return ('<?xml version="1.0"?><settings>{}</settings>'.format(values)).encode()


def _execute_json_rpc(request):
    payload = json.loads(request)
    method = payload["method"]
    if method == "Settings.GetSkinSettings":
        path = ENV.addon_data / ENV.skin / "settings.xml"
        settings = []
        if path.exists():
            import xml.etree.ElementTree as ET
            for item in ET.parse(path).getroot().findall("setting"):
                kind = item.get("type") or "string"
                settings.append({
                    "id": item.get("id"),
                    "type": "boolean" if kind == "bool" else "string",
                    "value": (item.text or "").lower() == "true" if kind == "bool" else (item.text or ""),
                })
        result = {"skin": ENV.skin, "settings": settings}
    else:
        result = True
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})


xbmc.executeJSONRPC = _execute_json_rpc


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        ENV.profile = self.root / "profile"
        ENV.addon_data = ENV.profile / "addon_data"
        ENV.skin = SKIN_ID
        ENV.settings = {
            "destination": str(self.root / "destination"),
            "device_name": "Living Room",
            "keep": 14,
            "enabled": True,
        }
        ENV.fail_record_write = False
        _Window.properties = {}
        _Dialog.notifications = []
        _DialogProgress.instances = []
        _DialogProgressBG.instances = []
        ENV.profile.mkdir()
        self.write_settings(3, "initial")
        self.app = runtime.App()

    def test_progress_dialog_closes_when_operation_fails(self):
        with self.assertRaisesRegex(runtime.BackupError, "deliberate failure"):
            with self.app.working("Starting"):
                self.app.progress(40, "Working")
                raise runtime.BackupError("deliberate failure")

        self.assertEqual(
            [("create", runtime.TITLE, "Starting"),
             ("update", 40, "Working"),
             ("close",)],
            _DialogProgress.instances[-1].events,
        )
        self.assertIsNone(self.app._progress)

    def test_vfs_stages_and_verifies_skin_settings_only_while_skin_is_inactive(self):
        values = [{"id": "family.setting", "type": "string", "value": "restored"}]
        ENV.skin = "skin.estuary"

        self.app.write_skin_settings_vfs(SKIN_ID, values)

        self.assertTrue(runtime.skin_settings_equal(
            self.app.saved_skin_setting_values(SKIN_ID), values))
        ENV.skin = SKIN_ID
        with self.assertRaisesRegex(runtime.BackupError, "must be inactive"):
            self.app.write_skin_settings_vfs(SKIN_ID, values)

    def test_failed_vfs_stage_restores_the_previous_document(self):
        values = [{"id": "family.setting", "type": "string", "value": "restored"}]
        path = ENV.addon_data / SKIN_ID / "settings.xml"
        previous = path.read_bytes()
        ENV.skin = "skin.estuary"
        original_write = self.app._write_vfs_bytes
        calls = []

        def fail_after_truncating(vfs_path, data):
            calls.append(vfs_path)
            if len(calls) == 1:
                Path(_translate_path(vfs_path)).write_bytes(b"partial")
                raise OSError("write failed")
            return original_write(vfs_path, data)

        with mock.patch.object(self.app, "_write_vfs_bytes", side_effect=fail_after_truncating):
            with self.assertRaisesRegex(runtime.BackupError, "stage and verify"):
                self.app.write_skin_settings_vfs(SKIN_ID, values)

        self.assertEqual(previous, path.read_bytes())

    def test_finish_restages_once_when_kodi_did_not_load_valid_pending_settings(self):
        values = [{"id": "family.setting", "type": "string", "value": "restored"}]
        runtime.atomic_json(self.app.pending_path, {
            "schema_version": 1,
            "skin_id": SKIN_ID,
            "phase": "rebuild",
            "created_at": "2026-09-07T12:00:00Z",
            "paths": [],
            "skin_settings": values,
            "helper_hashes": {},
        })

        with mock.patch.object(
                self.app, "load_restored_skin_settings",
                side_effect=[runtime.RestoredSettingsNotLoaded("not loaded"), None]) as load, \
                mock.patch.object(self.app, "restage_and_reactivate") as restage, \
                mock.patch.object(self.app, "verify_live_skin_settings"), \
                mock.patch.object(self.app, "verify_restored_helpers"):
            self.app.finish_restore()

        self.assertEqual(2, load.call_count)
        restage.assert_called_once()
        self.assertFalse(Path(self.app.pending_path).exists())

    def test_failed_restage_keeps_pending_transaction(self):
        values = [{"id": "family.setting", "type": "string", "value": "restored"}]
        runtime.atomic_json(self.app.pending_path, {
            "schema_version": 1,
            "skin_id": SKIN_ID,
            "phase": "rebuild",
            "created_at": "2026-09-07T12:00:00Z",
            "paths": [],
            "skin_settings": values,
            "helper_hashes": {},
        })

        with mock.patch.object(self.app, "load_restored_skin_settings",
                               side_effect=runtime.RestoredSettingsNotLoaded("not loaded")), \
                mock.patch.object(self.app, "restage_and_reactivate",
                                  side_effect=runtime.BackupError("VFS failed")):
            with self.assertRaisesRegex(runtime.BackupError, "VFS failed"):
                self.app.finish_restore()

        self.assertTrue(Path(self.app.pending_path).exists())

    def test_changed_document_is_not_automatically_restaged(self):
        runtime.atomic_json(self.app.pending_path, {
            "schema_version": 1,
            "skin_id": SKIN_ID,
            "phase": "rebuild",
            "created_at": "2026-09-07T12:00:00Z",
            "paths": [],
            "skin_settings": [],
            "helper_hashes": {},
        })

        with mock.patch.object(self.app, "load_restored_skin_settings",
                               side_effect=runtime.BackupError("document changed")), \
                mock.patch.object(self.app, "restage_and_reactivate") as restage:
            with self.assertRaisesRegex(runtime.BackupError, "document changed"):
                self.app.finish_restore()

        restage.assert_not_called()
        self.assertTrue(Path(self.app.pending_path).exists())

    def test_addon_declares_executable_before_background_service(self):
        import xml.etree.ElementTree as ET
        extensions = ET.parse(ROOT / "addon.xml").getroot().findall("extension")
        points = [item.get("point") for item in extensions]
        self.assertLess(points.index("xbmc.python.script"), points.index("xbmc.service"))
        script = next(item for item in extensions if item.get("point") == "xbmc.python.script")
        self.assertEqual("executable", script.findtext("provides"))

    def test_operation_continues_when_progress_dialog_is_unavailable(self):
        with mock.patch.object(runtime.xbmcgui, "DialogProgress", side_effect=RuntimeError("unsupported")), \
                mock.patch.object(runtime.xbmcgui, "DialogProgressBG", side_effect=RuntimeError("unsupported")):
            with self.app.working("Starting"):
                completed = True

        self.assertTrue(completed)

    def test_skin_switch_keeps_visible_cues_and_restores_modal_progress(self):
        ENV.skin = "skin.estuary"

        def change_skin(method, **params):
            self.assertEqual("Settings.SetSettingValue", method)
            ENV.skin = params["value"]
            return True

        with mock.patch.object(runtime, "rpc", side_effect=change_skin):
            with self.app.working("Restoring"):
                self.app.progress(35, "Before switch")
                self.app.switch_skin(SKIN_ID)
                self.assertIsNotNone(self.app._progress)
                self.assertFalse(self.app._progress_background)

        self.assertTrue(_Dialog.notifications)
        self.assertIn("Accept Kodi", _Dialog.notifications[-1][1])
        self.assertIn(("close",), _DialogProgressBG.instances[-1].events)
        self.assertGreaterEqual(len(_DialogProgress.instances), 2)
        self.assertIsNone(self.app._progress)

    def tearDown(self):
        self.temporary.cleanup()

    def write_settings(self, count, marker):
        path = ENV.addon_data / SKIN_ID / "settings.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_settings(count, marker))

    def records(self):
        return self.app.store(SKIN_ID, self.app.state()).records()

    def state_entry(self):
        state = json.loads(Path(self.app.state_path).read_text())
        return state["skins"][self.app.key(SKIN_ID)]

    def timestamp_sequence(self, count):
        base = datetime.datetime(2026, 9, 6, 12, 0, tzinfo=datetime.timezone.utc)
        fake_datetime = mock.Mock(wraps=datetime.datetime)
        fake_datetime.now.side_effect = [base + datetime.timedelta(minutes=i) for i in range(count)]
        return mock.patch.object(runtime.datetime, "datetime", fake_datetime)

    def test_scheduled_backup_twice_deduplicates_unchanged_files(self):
        first = self.app.backup()
        state_after_first = self.state_entry()

        second = self.app.backup()
        state_after_second = self.state_entry()

        self.assertTrue(first.startswith("Saved"))
        self.assertEqual("No changes since the last backup.", second)
        self.assertEqual(1, len(self.records()))
        self.assertEqual(state_after_first["archive"], state_after_second["archive"])
        self.assertEqual(state_after_first["last_backup"], state_after_second["last_backup"])

    def test_first_backup_is_always_protected(self):
        self.app.backup()

        records = self.records()
        self.assertEqual(1, len(records))
        self.assertIs(True, records[0]["manifest"]["protected"])

    def test_backup_asks_kodi_to_persist_skin_settings_first(self):
        settings_path = ENV.addon_data / SKIN_ID / "settings.xml"
        settings_path.unlink()

        def persist(command, _wait=False):
            if command == "Skin.SetString(service.skinsettings.backup.persist,1)":
                self.write_settings(3, "persisted")

        with mock.patch.object(runtime.xbmc, "executebuiltin", side_effect=persist) as execute:
            result = self.app.backup()

        self.assertTrue(result.startswith("Saved"))
        execute.assert_called_once_with("Skin.SetString(service.skinsettings.backup.persist,1)", True)
        self.assertEqual(3, self.state_entry()["stats"]["settings"])

    def test_backup_repairs_missing_settings_file_from_live_map(self):
        settings_path = ENV.addon_data / SKIN_ID / "settings.xml"
        settings_path.unlink()
        live = [
            {"id": "family.category", "type": "string", "value": "Movies"},
            {"id": "family.enabled", "type": "boolean", "value": True},
        ]

        with mock.patch.object(runtime, "live_skin_setting_values", return_value=live):
            result = self.app.backup()

        self.assertIn("Repaired settings.xml", result)
        self.assertEqual(live, self.app.saved_skin_setting_values(SKIN_ID))
        self.assertEqual(2, self.state_entry()["stats"]["settings"])

    def test_backup_uses_live_settings_instead_of_stale_disk_document(self):
        live = [
            {"id": "family.category", "type": "string", "value": "Movies"},
            {"id": "family.enabled", "type": "boolean", "value": True},
        ]
        with mock.patch.object(runtime, "live_skin_setting_values", return_value=live):
            result = self.app.backup()

        _manifest, files = runtime.read_archive(self.app.store(SKIN_ID, self.app.state()).load(self.records()[0]))
        values = runtime.skin_setting_values(files, SKIN_ID)
        self.assertEqual(live, values)
        self.assertIn("Saved 2 skin settings", result)

    def test_background_guard_does_not_rewrite_an_existing_valid_file(self):
        with mock.patch.object(runtime.xbmc, "executebuiltin") as execute:
            self.assertTrue(self.app.persist_skin_settings(SKIN_ID))

        execute.assert_not_called()

    def test_persistence_guard_repairs_a_valid_but_stale_document(self):
        live = [
            {"id": "current.category", "type": "string", "value": "Movies"},
            {"id": "current.enabled", "type": "boolean", "value": True},
        ]

        with mock.patch.object(runtime, "live_skin_setting_values", return_value=live):
            result = self.app.persist_skin_settings(SKIN_ID)

        self.assertEqual("repaired", result)
        self.assertEqual(live, self.app.saved_skin_setting_values(SKIN_ID))

    def test_persistence_fallback_uses_latest_stable_live_map(self):
        first = [{"id": "category", "type": "string", "value": "Old"}]
        latest = [{"id": "category", "type": "string", "value": "New"}]
        snapshots = [first, latest] + [latest] * 50

        with mock.patch.object(runtime, "live_skin_setting_values", side_effect=snapshots):
            result = self.app.persist_skin_settings(SKIN_ID)

        self.assertEqual("repaired", result)
        self.assertEqual(latest, self.app.saved_skin_setting_values(SKIN_ID))

    def test_persistence_refuses_to_write_while_live_map_keeps_changing(self):
        original = (ENV.addon_data / SKIN_ID / "settings.xml").read_bytes()
        snapshots = [[{"id": "changing", "type": "string", "value": str(index)}]
                     for index in range(50)]

        with mock.patch.object(runtime, "live_skin_setting_values", side_effect=snapshots):
            with self.assertRaisesRegex(runtime.BackupError, "kept changing"):
                self.app.persist_skin_settings(SKIN_ID)

        self.assertEqual(original, (ENV.addon_data / SKIN_ID / "settings.xml").read_bytes())

    def test_skin_setting_values_decodes_typed_archive_values(self):
        relative = "addon_data/{}/settings.xml".format(SKIN_ID)
        files = {relative: (b'<settings><setting id="alpha" type="string">Family</setting>'
                            b'<setting id="enabled" type="bool">true</setting></settings>')}

        self.assertEqual(
            [
                {"id": "alpha", "type": "string", "value": "Family"},
                {"id": "enabled", "type": "boolean", "value": True},
            ],
            runtime.skin_setting_values(files, SKIN_ID),
        )

    def test_restore_loads_complete_document_without_reset_or_per_setting_rpc(self):
        values = [
            {"id": "source.string", "type": "string", "value": "Family"},
            {"id": "source.bool", "type": "boolean", "value": False},
        ]
        settings_path = ENV.addon_data / SKIN_ID / "settings.xml"
        settings_path.write_bytes(runtime.skin_settings_document(values))

        with mock.patch.object(runtime.xbmc, "executebuiltin") as execute, \
                mock.patch.object(self.app, "verify_live_skin_settings") as verify:
            self.app.load_restored_skin_settings(SKIN_ID, values)

        execute.assert_not_called()
        verify.assert_called_once_with(SKIN_ID, values)

    def test_appearance_is_captured_and_appearance_only_change_creates_backup(self):
        appearance = {
            "lookandfeel.skintheme": "SKINDEFAULT",
            "lookandfeel.skincolors": "Charcoal",
            "lookandfeel.font": "Default",
            "lookandfeel.skinzoom": 0,
        }
        with self.timestamp_sequence(2):
            with mock.patch.object(self.app, "appearance", side_effect=lambda: dict(appearance)):
                self.app.backup()
                first_state = self.state_entry()
                appearance["lookandfeel.skincolors"] = "Light"
                result = self.app.backup()

        records = self.records()
        self.assertTrue(result.startswith("Saved"))
        self.assertEqual(2, len(records))
        self.assertNotEqual(first_state["fingerprint"], self.state_entry()["fingerprint"])
        self.assertEqual("Light", records[0]["manifest"]["appearance"]["lookandfeel.skincolors"])
        self.assertEqual("Charcoal", records[1]["manifest"]["appearance"]["lookandfeel.skincolors"])

    def test_lowered_retention_keeps_first_protected_and_latest_ordinary(self):
        with self.timestamp_sequence(4):
            for index in range(3):
                self.write_settings(3, "version{}".format(index))
                self.app.backup()
            ENV.settings["keep"] = 1
            self.write_settings(3, "version3")
            self.app.backup()

        records = self.records()
        self.assertEqual(2, len(records))
        self.assertEqual([False, True], sorted(record["manifest"]["protected"] for record in records))
        ordinary = next(record for record in records if not record["manifest"]["protected"])
        self.assertEqual("2026-09-06T12:03:00Z", ordinary["manifest"]["created_at"])

    def test_suspected_reset_pauses_automatic_backup(self):
        self.write_settings(12, "configured")
        self.app.backup()
        previous_state = self.state_entry()
        previous_records = self.records()
        self.write_settings(1, "reset")

        with self.assertRaisesRegex(runtime.BackupError, "Automatic backups are paused"):
            self.app.backup()

        blocked_state = self.state_entry()
        self.assertEqual(previous_state, {key: value for key, value in blocked_state.items() if key != "blocked"})
        self.assertIn("Automatic backups are paused", blocked_state["blocked"])
        self.assertEqual(previous_records, self.records())

        with mock.patch.object(runtime, "collect_files") as collect:
            with self.assertRaisesRegex(runtime.BackupError, "Automatic backups are paused"):
                self.app.backup()
        collect.assert_not_called()

        result = self.app.backup(manual=True)
        self.assertTrue(result.startswith("Saved"))
        self.assertNotIn("blocked", self.state_entry())
        self.assertEqual(2, len(self.records()))

    def test_failed_publication_does_not_update_last_successful_state(self):
        self.app.backup()
        previous_state = self.state_entry()
        previous_records = self.records()
        self.write_settings(3, "changed")
        ENV.fail_record_write = True

        with self.assertRaises(runtime.BackupError):
            self.app.backup()

        self.assertEqual(previous_state, self.state_entry())
        self.assertEqual(previous_records, self.records())

    def test_pending_restore_blocks_backup_before_collecting_files(self):
        Path(self.app.pending_path).write_text('{"skin_id":"skin.example","phase":"rebuild"}')

        with mock.patch.object(runtime, "collect_files") as collect:
            with self.assertRaisesRegex(runtime.BackupError, "pending restore"):
                self.app.backup()

        collect.assert_not_called()

    def test_restore_rechecks_active_skin_after_switch_returns_without_switching(self):
        original = _settings(3, "original")
        self.write_settings(3, "original")
        metadata = {
            "skin_id": SKIN_ID,
            "skin_version": "3.0.0",
            "device_id": "other-device",
            "device_name": "Other Kodi",
            "profile_id": "master",
            "created_at": "2026-09-06T12:00:00Z",
            "protected": True,
        }
        incoming = {"addon_data/{}/settings.xml".format(SKIN_ID): _settings(3, "incoming")}
        blob = runtime.build_archive(incoming, metadata)

        with mock.patch.object(self.app, "switch_skin", return_value=None), \
                mock.patch.object(runtime, "restore_files", wraps=runtime.restore_files) as restore:
            with self.assertRaisesRegex(runtime.BackupError, "target skin is still active"):
                self.app.restore_blob(blob)

        restore.assert_not_called()
        self.assertEqual(original, (ENV.addon_data / SKIN_ID / "settings.xml").read_bytes())
        self.assertFalse(Path(self.app.pending_path).exists())

    def test_corrupt_transaction_journal_is_detected(self):
        transaction = Path(self.app.rollback_root) / "skin.example-transaction" / "transaction.json"
        transaction.parent.mkdir(parents=True)
        transaction.write_text("{not-json")

        with self.assertRaisesRegex(runtime.BackupError, "state file is damaged"):
            self.app.incomplete()

    def test_foreign_profile_transaction_does_not_block_operations(self):
        transaction = Path(self.app.rollback_root) / "foreign" / "transaction.json"
        transaction.parent.mkdir(parents=True)
        transaction.write_text(json.dumps({"version": 1, "profile_path": "/another/profile", "status": "applying"}))

        self.assertFalse(self.app.incomplete())

    def test_wrong_json_shapes_are_rejected_without_replacement(self):
        Path(self.app.state_path).write_text("[]")

        with self.assertRaisesRegex(runtime.BackupError, "state file is damaged"):
            self.app.state()

        self.assertEqual("[]", Path(self.app.state_path).read_text())

    def test_unknown_pending_schema_is_preserved_and_rejected(self):
        runtime.atomic_json(self.app.pending_path, {
            "schema_version": 99,
            "skin_id": SKIN_ID,
            "phase": "rebuild",
            "paths": [],
        })

        with self.assertRaisesRegex(runtime.BackupError, "pending restore state is invalid"):
            self.app.pending()

        self.assertEqual(99, json.loads(Path(self.app.pending_path).read_text())["schema_version"])

    def test_ambiguous_recovery_preserves_pending_state(self):
        runtime.atomic_json(self.app.pending_path, {
            "skin_id": SKIN_ID,
            "phase": "restoring",
            "created_at": "2026-09-06T12:00:00Z",
            "paths": ["addon_data/{}/settings.xml".format(SKIN_ID)],
            "started_at": time.time(),
            "known_transactions": [],
        })
        ENV.skin = "skin.estuary"

        with self.assertRaisesRegex(runtime.BackupError, "Pending state was preserved"):
            self.app.recovery()

        self.assertTrue(Path(self.app.pending_path).exists())

    def test_recovery_clears_pending_after_rolling_back_interrupted_write(self):
        settings_path = "addon_data/{}/settings.xml".format(SKIN_ID)
        original = (ENV.profile / settings_path).read_bytes()
        rollback = Path(runtime.restore_files(
            ENV.profile, SKIN_ID, {settings_path: _settings(2, "restored")}, self.app.rollback_root))
        journal_path = rollback / "transaction.json"
        journal = json.loads(journal_path.read_text())
        journal["status"] = "applying"
        journal_path.write_text(json.dumps(journal))
        runtime.atomic_json(self.app.pending_path, {
            "skin_id": SKIN_ID,
            "phase": "restoring",
            "created_at": "2026-09-06T12:00:00Z",
            "paths": [settings_path],
        })
        ENV.skin = "skin.estuary"

        self.app.recovery()

        self.assertFalse(Path(self.app.pending_path).exists())
        self.assertEqual(original, (ENV.profile / settings_path).read_bytes())

    def test_cancel_staged_restore_puts_back_and_verifies_previous_files(self):
        settings_path = "addon_data/{}/settings.xml".format(SKIN_ID)
        original = (ENV.profile / settings_path).read_bytes()
        rollback = runtime.restore_files(
            ENV.profile, SKIN_ID, {settings_path: _settings(2, "restored")}, self.app.rollback_root)
        runtime.atomic_json(self.app.pending_path, {
            "skin_id": SKIN_ID,
            "phase": "rebuild",
            "created_at": "2026-09-06T12:00:00Z",
            "paths": [settings_path],
            "rollback": rollback,
            "previous_appearance": {"lookandfeel.skincolors": "charcoal"},
        })
        ENV.skin = "skin.estuary"

        self.app.cancel_restore()

        self.assertEqual(original, (ENV.profile / settings_path).read_bytes())
        pending = json.loads(Path(self.app.pending_path).read_text())
        self.assertEqual("rollback_rebuild", pending["phase"])
        self.assertEqual("charcoal", pending["appearance"]["lookandfeel.skincolors"])
        journal = json.loads((Path(rollback) / "transaction.json").read_text())
        self.assertEqual("rolled_back", journal["status"])
        pending["appearance"] = {}
        runtime.atomic_json(self.app.pending_path, pending)
        ENV.skin = SKIN_ID
        self.app.finish_restore()
        self.assertFalse(Path(self.app.pending_path).exists())

    def test_cancel_can_resume_if_pending_update_was_interrupted_after_rollback(self):
        settings_path = "addon_data/{}/settings.xml".format(SKIN_ID)
        rollback = runtime.restore_files(
            ENV.profile, SKIN_ID, {settings_path: _settings(2, "restored")}, self.app.rollback_root)
        runtime.atomic_json(self.app.pending_path, {
            "skin_id": SKIN_ID,
            "phase": "rebuild",
            "created_at": "2026-09-06T12:00:00Z",
            "paths": [settings_path],
            "rollback": rollback,
        })
        ENV.skin = "skin.estuary"

        with mock.patch.object(runtime, "atomic_json", side_effect=OSError("power loss")):
            with self.assertRaises(OSError):
                self.app.cancel_restore()
        self.assertEqual("rebuild", json.loads(Path(self.app.pending_path).read_text())["phase"])
        self.assertEqual("rolled_back", json.loads((Path(rollback) / "transaction.json").read_text())["status"])

        self.app.cancel_restore()
        self.assertEqual("rollback_rebuild", json.loads(Path(self.app.pending_path).read_text())["phase"])

    def test_abandon_restore_clears_stuck_pending_without_touching_files(self):
        settings_path = "addon_data/{}/settings.xml".format(SKIN_ID)
        original = (ENV.profile / settings_path).read_bytes()
        runtime.atomic_json(
            self.app.pending_path,
            {
                "skin_id": SKIN_ID,
                "phase": "rollback_rebuild",
                "created_at": "2026-09-06T12:00:00Z",
                "paths": [settings_path],
                "appearance": {},
            },
        )
        ENV.skin = "skin.estuary"

        self.app.abandon_restore()

        self.assertFalse(Path(self.app.pending_path).exists())
        self.assertEqual(original, (ENV.profile / settings_path).read_bytes())
        archived = list(Path(self.app.abandoned_root).glob("*.json"))
        self.assertEqual(1, len(archived))
        self.assertEqual("rollback_rebuild", json.loads(archived[0].read_text())["pending"]["phase"])

    def test_af3_rebuild_verifies_generated_xml_around_synchronous_reload(self):
        ENV.skin = runtime.AF3
        skin_path = ENV.addon_data / runtime.AF3
        generated = (
            "script-skinvariables-includes.xml",
            "script-skinvariables-labels-includes.xml",
            "script-skinvariables-images-includes.xml",
        )
        for name in generated:
            path = skin_path / "1080i" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"<includes />")

        calls = []
        def execute(command, wait=False):
            calls.append((command, wait))
            if command.startswith("RunScript("):
                plan = json.loads((Path(self.app.data) / "rebuild.json").read_text())
                token = plan["actions"][-1].split(",")[1]
                _Window.properties["SkinSettingsBackup.RebuildComplete"] = token

        with mock.patch.object(runtime.xbmc, "getInfoLabel", return_value=""), \
                mock.patch.object(runtime.xbmc, "executebuiltin", side_effect=execute):
            self.app.rebuild_af3({"skin_id": runtime.AF3})

        self.assertIn(("ReloadSkin()", True), calls)

    def test_af3_rebuild_keeps_pending_work_when_generated_xml_is_missing(self):
        ENV.skin = runtime.AF3
        calls = []
        def execute(command, wait=False):
            calls.append((command, wait))
            if command.startswith("RunScript("):
                plan = json.loads((Path(self.app.data) / "rebuild.json").read_text())
                token = plan["actions"][-1].split(",")[1]
                _Window.properties["SkinSettingsBackup.RebuildComplete"] = token

        with mock.patch.object(runtime.xbmc, "getInfoLabel", return_value=""), \
                mock.patch.object(runtime.xbmc, "executebuiltin", side_effect=execute):
            with self.assertRaisesRegex(runtime.BackupError, "generation was incomplete"):
                self.app.rebuild_af3({"skin_id": runtime.AF3})

        self.assertNotIn(("ReloadSkin()", True), calls)

    def test_recovery_advances_phase_when_restore_completed_before_pending_update(self):
        old_helper = "addon_data/script.skinvariables/nodes/{}/old.json".format(SKIN_ID)
        old_helper_path = ENV.profile / old_helper
        old_helper_path.parent.mkdir(parents=True, exist_ok=True)
        old_helper_path.write_bytes(b'{"old":true}')
        incoming_path = "addon_data/{}/settings.xml".format(SKIN_ID)
        incoming = {incoming_path: _settings(3, "restored")}
        started_at = time.time() - 1
        rollback = runtime.restore_files(ENV.profile, SKIN_ID, incoming, self.app.rollback_root)
        runtime.atomic_json(
            self.app.pending_path,
            {
                "skin_id": SKIN_ID,
                "phase": "restoring",
                "created_at": "2026-09-06T12:00:00Z",
                "paths": [incoming_path],
                "started_at": started_at,
            },
        )
        ENV.skin = "skin.estuary"

        self.app.recovery()

        pending = json.loads(Path(self.app.pending_path).read_text())
        self.assertEqual("rebuild", pending["phase"])
        self.assertEqual(rollback, pending["rollback"])
        self.assertEqual(sorted([incoming_path, old_helper]), pending["paths"])
        self.assertEqual(incoming[incoming_path], (ENV.profile / incoming_path).read_bytes())
        self.assertFalse(old_helper_path.exists())

    def test_recovery_uses_transaction_inventory_when_clock_moves_backwards(self):
        incoming_path = "addon_data/{}/settings.xml".format(SKIN_ID)
        old_rollback = Path(
            runtime.restore_files(
                ENV.profile,
                SKIN_ID,
                {incoming_path: _settings(3, "older-restore")},
                self.app.rollback_root,
            )
        )
        known_transactions = sorted(path.name for path in Path(self.app.rollback_root).iterdir())
        old_helper = "addon_data/script.skinvariables/nodes/{}/old.json".format(SKIN_ID)
        old_helper_path = ENV.profile / old_helper
        old_helper_path.parent.mkdir(parents=True, exist_ok=True)
        old_helper_path.write_bytes(b'{"old":true}')
        self.write_settings(3, "before-new-restore")
        incoming = {incoming_path: _settings(3, "new-restored")}
        new_rollback = Path(runtime.restore_files(ENV.profile, SKIN_ID, incoming, self.app.rollback_root))
        # Both journals predate started_at, and the unrelated old transaction looks newer.
        # Directory inventory must identify the transaction created by this restore.
        os.utime(new_rollback / "transaction.json", (100, 100))
        os.utime(old_rollback / "transaction.json", (200, 200))
        runtime.atomic_json(
            self.app.pending_path,
            {
                "skin_id": SKIN_ID,
                "phase": "restoring",
                "created_at": "2026-09-06T12:00:00Z",
                "paths": [incoming_path],
                "started_at": 300,
                "known_transactions": known_transactions,
            },
        )
        ENV.skin = "skin.estuary"

        self.app.recovery()

        pending = json.loads(Path(self.app.pending_path).read_text())
        self.assertEqual("rebuild", pending["phase"])
        self.assertEqual(str(new_rollback), pending["rollback"])
        self.assertEqual(sorted([incoming_path, old_helper]), pending["paths"])
        self.assertEqual(incoming[incoming_path], (ENV.profile / incoming_path).read_bytes())
        self.assertFalse(old_helper_path.exists())

    def test_finish_restore_replays_cache_clear_and_resets_skin_state(self):
        node_path = "addon_data/script.skinvariables/nodes/{}/movies/main.json".format(SKIN_ID)
        cache_property = "SkinVariables.ShortcutsNode.{}/movies-main.json".format(SKIN_ID)
        _Window.properties[cache_property] = "stale"
        runtime.atomic_json(
            self.app.state_path,
            {"device_id": self.app.device_id, "skins": {"old-destination": {"last_backup": "old"}}},
        )
        runtime.atomic_json(
            self.app.pending_path,
            {
                "skin_id": SKIN_ID,
                "phase": "rebuild",
                "created_at": "2026-09-06T12:00:00Z",
                "paths": ["addon_data/{}/settings.xml".format(SKIN_ID), node_path],
            },
        )

        self.app.finish_restore()

        state = json.loads(Path(self.app.state_path).read_text())
        self.assertEqual(self.app.device_id, state["device_id"])
        self.assertEqual({}, state["skins"])
        self.assertNotIn(cache_property, _Window.properties)
        self.assertTrue(_Window.properties["SkinVariables.ShortcutsNode.Reload"])
        self.assertFalse(Path(self.app.pending_path).exists())

    def test_finish_restore_applies_live_settings_before_af3_rebuild(self):
        ENV.skin = runtime.AF3
        values = [{"id": "family.setting", "type": "string", "value": "restored"}]
        runtime.atomic_json(
            self.app.pending_path,
            {
                "skin_id": runtime.AF3,
                "phase": "rebuild",
                "created_at": "2026-09-06T12:00:00Z",
                "paths": [],
                "skin_settings": values,
                "helper_hashes": {},
            },
        )
        order = []

        with mock.patch.object(self.app, "load_restored_skin_settings",
                               side_effect=lambda skin, settings: order.append(("load", skin, settings))), \
                mock.patch.object(self.app, "clear_helper_cache",
                                  side_effect=lambda skin, paths: order.append(("clear", skin, paths))), \
                mock.patch.object(self.app, "rebuild_af3",
                                  side_effect=lambda pending: order.append(("rebuild", pending["skin_id"]))), \
                mock.patch.object(self.app, "verify_live_skin_settings",
                                  side_effect=lambda skin, settings: order.append(("verify", skin, settings))):
            self.app.finish_restore()

        self.assertEqual(
            [
                ("load", runtime.AF3, values),
                ("clear", runtime.AF3, []),
                ("rebuild", runtime.AF3),
                ("verify", runtime.AF3, values),
            ],
            order,
        )

    def test_finish_restore_rejects_helper_file_changed_during_rebuild(self):
        ENV.skin = runtime.AF3
        helper = "addon_data/script.skinvariables/nodes/{}/menu.json".format(runtime.AF3)
        helper_path = ENV.profile / helper
        helper_path.parent.mkdir(parents=True, exist_ok=True)
        helper_path.write_bytes(b'{"source":true}')
        runtime.atomic_json(
            self.app.pending_path,
            {
                "skin_id": runtime.AF3,
                "phase": "rebuild",
                "created_at": "2026-09-06T12:00:00Z",
                "paths": [helper],
                "skin_settings": [],
                "helper_hashes": {helper: __import__("hashlib").sha256(b'{"source":true}').hexdigest()},
            },
        )

        def overwrite(_pending):
            helper_path.write_bytes(b'{"target":true}')

        with mock.patch.object(self.app, "load_restored_skin_settings"), \
                mock.patch.object(self.app, "rebuild_af3", side_effect=overwrite):
            with self.assertRaisesRegex(runtime.BackupError, "did not remain applied"):
                self.app.finish_restore()

        self.assertTrue(Path(self.app.pending_path).exists())

    def test_finish_restore_applies_only_allowed_appearance_in_defined_order(self):
        appearance = {
            "lookandfeel.skinzoom": 10,
            "lookandfeel.font": "Arial",
            "unknown.setting": "must-not-be-applied",
            "lookandfeel.skincolors": "Light",
            "lookandfeel.skintheme": "Textures.xbt",
        }
        runtime.atomic_json(
            self.app.pending_path,
            {
                "skin_id": SKIN_ID,
                "phase": "rebuild",
                "created_at": "2026-09-06T12:00:00Z",
                "paths": [],
                "appearance": appearance,
            },
        )
        restore_order = (
            "lookandfeel.skintheme",
            "lookandfeel.skincolors",
            "lookandfeel.font",
            "lookandfeel.skinzoom",
        )
        restored_values = {setting: appearance[setting] for setting in restore_order}

        def setting_rpc(method, **params):
            if method == "Settings.GetSettingValue":
                return {"value": "current-" + params["setting"]}
            if method == "Settings.SetSettingValue":
                return True
            self.fail("unexpected RPC method: {}".format(method))

        with mock.patch.object(runtime, "rpc", side_effect=setting_rpc) as rpc:
            self.app.finish_restore()

        expected = []
        for setting in restore_order:
            expected.extend(
                [
                    mock.call("Settings.GetSettingValue", setting=setting),
                    mock.call("Settings.SetSettingValue", setting=setting, value=restored_values[setting]),
                ]
            )
        self.assertEqual(expected, rpc.call_args_list)
        self.assertFalse(Path(self.app.pending_path).exists())


if __name__ == "__main__":
    unittest.main()
