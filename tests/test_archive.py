import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resources" / "lib"))
import archive  # noqa: E402


SKIN_ID = "skin.example"
SETTINGS = b'<?xml version="1.0"?><settings><setting id="theme">dark</setting></settings>'


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile = self.root / "profile"
        self.rollback = self.root / "rollback"
        self.profile.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, data):
        path = self.profile / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def metadata(self):
        return {
            "skin_id": SKIN_ID,
            "skin_version": "3.0.0",
            "device_id": "living-room",
            "device_name": "Living Room",
            "profile_id": "master",
            "created_at": "2026-09-06T14:00:00Z",
            "protected": True,
        }

    def test_collect_build_and_read_round_trip(self):
        self.write(f"addon_data/{SKIN_ID}/settings.xml", SETTINGS)
        self.write(f"addon_data/script.skinvariables/nodes/{SKIN_ID}/movies/main.json", b'{"label":"Movies"}')
        self.write(
            f"addon_data/script.skinvariables/logins/{SKIN_ID}/skinusers.json",
            b'[{"name":"Family","slug":"user-Family"}]',
        )
        self.write(
            f"addon_data/script.skinvariables/nodes/{SKIN_ID}-user-Family/tv/main.json",
            b'{"label":"Family TV"}',
        )
        self.write(f"addon_data/script.skinvariables/logins/{SKIN_ID}/account.json", b'{"token":"redacted"}')
        self.write(f"addon_data/script.skinvariables/{SKIN_ID}-viewtypes.json", b'{"view":50}')
        self.write("addon_data/unrelated.addon/settings.xml", SETTINGS)

        files = archive.collect_files(self.profile, SKIN_ID)
        blob = archive.build_archive(files, self.metadata())
        manifest, unpacked = archive.read_archive(blob)

        self.assertEqual(files, unpacked)
        self.assertEqual(1, manifest["schema_version"])
        self.assertEqual(self.metadata()["device_id"], manifest["device_id"])
        self.assertNotIn("addon_data/unrelated.addon/settings.xml", unpacked)
        self.assertEqual(sorted(unpacked), [entry["path"] for entry in manifest["entries"]])

    def test_collects_helper_data_when_kodi_has_no_skin_settings_file(self):
        helper = f"addon_data/script.skinvariables/nodes/{SKIN_ID}/movies/main.json"
        self.write(helper, b'{"label":"Movies"}')

        files = archive.collect_files(self.profile, SKIN_ID)
        settings = f"addon_data/{SKIN_ID}/settings.xml"

        self.assertEqual(b'{"label":"Movies"}', files[helper])
        self.assertEqual("settings", archive.ElementTree.fromstring(files[settings]).tag)
        self.assertEqual([], archive.ElementTree.fromstring(files[settings]).findall("setting"))
        manifest, unpacked = archive.read_archive(archive.build_archive(files, self.metadata()))
        self.assertEqual(files, unpacked)
        self.assertEqual(2, len(manifest["entries"]))

    def test_appearance_metadata_is_preserved_and_defaults_empty(self):
        files = {f"addon_data/{SKIN_ID}/settings.xml": SETTINGS}
        metadata = self.metadata()
        metadata["appearance"] = {
            "lookandfeel.skintheme": "SKINDEFAULT",
            "lookandfeel.skincolors": "Dark",
            "lookandfeel.font": "Default",
            "lookandfeel.skinzoom": -4,
        }
        manifest, _ = archive.read_archive(archive.build_archive(files, metadata))
        self.assertEqual(metadata["appearance"], manifest["appearance"])

        manifest, _ = archive.read_archive(archive.build_archive(files, self.metadata()))
        self.assertEqual({}, manifest["appearance"])

        modern = zipfile.ZipFile(io.BytesIO(archive.build_archive(files, self.metadata())))
        legacy_blob = io.BytesIO()
        with modern, zipfile.ZipFile(legacy_blob, "w") as legacy:
            for info in modern.infolist():
                data = modern.read(info)
                if info.filename == "manifest.json":
                    old_manifest = json.loads(data)
                    old_manifest.pop("appearance")
                    data = json.dumps(old_manifest).encode()
                legacy.writestr(info, data)
        manifest, _ = archive.read_archive(legacy_blob.getvalue())
        self.assertEqual({}, manifest["appearance"])

    def test_appearance_metadata_rejects_unknown_and_invalid_values(self):
        files = {f"addon_data/{SKIN_ID}/settings.xml": SETTINGS}
        invalid = (
            {"unknown.setting": "value"},
            {"lookandfeel.skintheme": ""},
            {"lookandfeel.skincolors": "x" * 257},
            {"lookandfeel.skinzoom": True},
            {"lookandfeel.skinzoom": 101},
        )
        for appearance in invalid:
            with self.subTest(appearance=appearance):
                metadata = self.metadata()
                metadata["appearance"] = appearance
                with self.assertRaises(archive.BackupError):
                    archive.build_archive(files, metadata)

    def test_archive_accepts_safe_inferred_skin_user_directory(self):
        files = {
            f"addon_data/{SKIN_ID}/settings.xml": SETTINGS,
            f"addon_data/script.skinvariables/logins/{SKIN_ID}/skinusers.json": b"[]",
            f"addon_data/script.skinvariables/nodes/{SKIN_ID}-user-Invented/main.json": b"{}",
        }
        _manifest, restored = archive.read_archive(archive.build_archive(files, self.metadata()))
        self.assertEqual(files, restored)

    def test_collects_inferred_skin_user_directory_without_declaration(self):
        self.write(f"addon_data/{SKIN_ID}/settings.xml", SETTINGS)
        profile_file = (
            f"addon_data/script.skinvariables/nodes/{SKIN_ID}-user-Family/"
            "skinvariables-shortcut-homewidgets.json"
        )
        self.write(profile_file, b'[{"label":"Family widget"}]')

        files = archive.collect_files(self.profile, SKIN_ID)

        self.assertEqual(b'[{"label":"Family widget"}]', files[profile_file])

    def test_rejects_nested_setting_and_ntfs_stream_path(self):
        with self.assertRaises(archive.BackupError):
            archive.build_archive(
                {f"addon_data/{SKIN_ID}/settings.xml": b"<settings><group><setting id='x'/></group></settings>"},
                self.metadata(),
            )
        with self.assertRaises(archive.BackupError):
            archive.build_archive(
                {
                    f"addon_data/{SKIN_ID}/settings.xml": SETTINGS,
                    f"addon_data/script.skinvariables/nodes/{SKIN_ID}/item.json:stream": b"{}",
                },
                self.metadata(),
            )

    def test_rejects_corrupt_checksum_and_traversal_member(self):
        files = {f"addon_data/{SKIN_ID}/settings.xml": SETTINGS}
        blob = archive.build_archive(files, self.metadata())
        source = zipfile.ZipFile(io.BytesIO(blob))
        altered = io.BytesIO()
        with source, zipfile.ZipFile(altered, "w") as target:
            for info in source.infolist():
                data = source.read(info)
                if info.filename.endswith("settings.xml"):
                    data += b" "
                target.writestr(info, data)
        with self.assertRaises(archive.BackupError):
            archive.read_archive(altered.getvalue())

        unsafe = io.BytesIO()
        with zipfile.ZipFile(unsafe, "w") as target:
            target.writestr("manifest.json", b"{}")
            target.writestr("../outside", b"bad")
        with self.assertRaises(archive.BackupError):
            archive.read_archive(unsafe.getvalue())

    def test_rejects_duplicate_zip_members(self):
        duplicate = io.BytesIO()
        with zipfile.ZipFile(duplicate, "w") as target:
            target.writestr("manifest.json", b"{}")
            with self.assertWarns(UserWarning):
                target.writestr("manifest.json", b"{}")
        with self.assertRaises(archive.BackupError):
            archive.read_archive(duplicate.getvalue())

    def test_restore_removes_stale_helper_json_and_keeps_unmanaged_files(self):
        old_settings = self.write(f"addon_data/{SKIN_ID}/settings.xml", SETTINGS.replace(b"dark", b"old"))
        self.write(
            f"addon_data/script.skinvariables/logins/{SKIN_ID}/skinusers.json",
            b'[{"name":"Old","slug":"user-Old"}]',
        )
        stale = self.write(f"addon_data/script.skinvariables/nodes/{SKIN_ID}-user-Old/stale.json", b'{"old":true}')
        unmanaged = self.write(f"addon_data/script.skinvariables/nodes/{SKIN_ID}/notes.txt", b"keep")
        files = {
            f"addon_data/{SKIN_ID}/settings.xml": SETTINGS,
            f"addon_data/script.skinvariables/nodes/{SKIN_ID}/fresh.json": b'{"fresh":true}',
        }

        rollback_directory = archive.restore_files(self.profile, SKIN_ID, files, self.rollback)

        self.assertEqual(SETTINGS, old_settings.read_bytes())
        self.assertFalse(stale.exists())
        self.assertTrue(unmanaged.exists())
        self.assertTrue((self.profile / f"addon_data/script.skinvariables/nodes/{SKIN_ID}/fresh.json").exists())
        journal = json.loads((Path(rollback_directory) / "transaction.json").read_text())
        self.assertEqual("complete", journal["status"])
        self.assertTrue((Path(rollback_directory) / "files" / f"addon_data/{SKIN_ID}/settings.xml").exists())

    def test_restore_repairs_corrupt_skinusers_and_captures_inferred_profile(self):
        self.write(f"addon_data/{SKIN_ID}/settings.xml", SETTINGS.replace(b"dark", b"old"))
        declaration = self.write(
            f"addon_data/script.skinvariables/logins/{SKIN_ID}/skinusers.json", b"not json"
        )
        stale = self.write(
            f"addon_data/script.skinvariables/nodes/{SKIN_ID}-user-Orphan/main.json", b'{"old":true}'
        )
        files = {
            f"addon_data/{SKIN_ID}/settings.xml": SETTINGS,
            f"addon_data/script.skinvariables/logins/{SKIN_ID}/skinusers.json": b"[]",
        }

        rollback_directory = Path(archive.restore_files(self.profile, SKIN_ID, files, self.rollback))

        self.assertFalse(stale.exists())
        self.assertEqual(b"[]", declaration.read_bytes())
        self.assertEqual(
            b"not json",
            (rollback_directory / "files" / f"addon_data/script.skinvariables/logins/{SKIN_ID}/skinusers.json").read_bytes(),
        )

    def test_failed_restore_automatically_rolls_back_deletion_and_writes(self):
        original_settings = SETTINGS.replace(b"dark", b"original")
        settings_path = self.write(f"addon_data/{SKIN_ID}/settings.xml", original_settings)
        stale_path = self.write(f"addon_data/script.skinvariables/nodes/{SKIN_ID}/stale.json", b'{"old":true}')
        files = {
            f"addon_data/{SKIN_ID}/settings.xml": SETTINGS,
            f"addon_data/script.skinvariables/nodes/{SKIN_ID}/fresh.json": b'{"fresh":true}',
        }
        with mock.patch.object(archive, "_atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(archive.BackupError):
                archive.restore_files(self.profile, SKIN_ID, files, self.rollback)

        self.assertEqual(original_settings, settings_path.read_bytes())
        self.assertEqual(b'{"old":true}', stale_path.read_bytes())
        self.assertFalse((self.profile / f"addon_data/script.skinvariables/nodes/{SKIN_ID}/fresh.json").exists())
        journals = list(self.rollback.glob("*/transaction.json"))
        self.assertEqual(1, len(journals))
        self.assertEqual("rolled_back", json.loads(journals[0].read_text())["status"])

    def test_symlink_file_and_parent_are_rejected(self):
        outside = self.root / "outside.xml"
        outside.write_bytes(SETTINGS)
        settings = self.profile / f"addon_data/{SKIN_ID}/settings.xml"
        settings.parent.mkdir(parents=True)
        settings.symlink_to(outside)
        with self.assertRaises(archive.BackupError):
            archive.collect_files(self.profile, SKIN_ID)

        settings.unlink()
        settings.write_bytes(SETTINGS)
        helper_parent = self.profile / "addon_data/script.skinvariables/nodes"
        helper_parent.mkdir(parents=True)
        (helper_parent / SKIN_ID).symlink_to(self.root)
        with self.assertRaises(archive.BackupError):
            archive.collect_files(self.profile, SKIN_ID)

    def test_recover_pending_restores_snapshot(self):
        original = SETTINGS.replace(b"dark", b"original")
        settings = self.write(f"addon_data/{SKIN_ID}/settings.xml", original)
        files = {f"addon_data/{SKIN_ID}/settings.xml": SETTINGS}
        rollback_directory = Path(archive.restore_files(self.profile, SKIN_ID, files, self.rollback))
        journal_path = rollback_directory / "transaction.json"
        journal = json.loads(journal_path.read_text())
        journal["status"] = "applying"
        journal_path.write_text(json.dumps(journal))

        recovered = archive.recover_pending(self.profile, self.rollback)

        self.assertEqual([str(rollback_directory)], recovered)
        self.assertEqual(original, settings.read_bytes())
        self.assertEqual("rolled_back", json.loads(journal_path.read_text())["status"])


if __name__ == "__main__":
    unittest.main()
