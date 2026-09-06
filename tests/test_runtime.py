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
    def yesno(self, _title, _message):
        return True

    def ok(self, _title, _message):
        return None

    def notification(self, *_args):
        return None


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
        self.path = Path(path)
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
xbmcgui.Window = _Window

xbmcaddon = types.ModuleType("xbmcaddon")
xbmcaddon.Addon = _Addon

xbmcvfs = types.ModuleType("xbmcvfs")
xbmcvfs.translatePath = _translate_path
xbmcvfs.File = _VFSFile
xbmcvfs.exists = lambda path: Path(path).exists()
xbmcvfs.mkdirs = lambda path: (Path(path).mkdir(parents=True, exist_ok=True) or True)
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
        ENV.profile.mkdir()
        self.write_settings(3, "initial")
        self.app = runtime.App()

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

        self.assertIn("Repaired the missing settings.xml", result)
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
