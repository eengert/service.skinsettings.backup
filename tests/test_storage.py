import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from resources.lib.archive import BackupError, build_archive  # noqa: E402
from resources.lib.storage import Store  # noqa: E402


class FakeFile:
    def __init__(self, path, mode):
        self.path = Path(path)
        self.mode = mode
        self.handle = open(self.path, "wb" if "w" in mode else "rb")

    def size(self):
        return self.path.stat().st_size

    def readBytes(self, count):
        return self.handle.read(count)

    def write(self, data):
        self.handle.write(data)
        self.handle.flush()
        return True

    def close(self):
        self.handle.close()


class FakeVFS:
    def __init__(self):
        self.copy_allowed = True

    def File(self, path, mode=None):
        return FakeFile(path, mode or "r")

    def exists(self, path):
        return Path(path.rstrip("/\\")).exists()

    def mkdirs(self, path):
        Path(path.rstrip("/\\")).mkdir(parents=True, exist_ok=True)
        return True

    def listdir(self, path):
        directory = Path(path)
        return ([str(item) for item in directory.iterdir() if item.is_dir()],
                [item.name for item in directory.iterdir() if item.is_file()])

    def copy(self, source, target):
        if not self.copy_allowed:
            return False
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(source).read_bytes())
        return True

    def delete(self, path):
        target = Path(path)
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return False


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "backups"
        self.staging = Path(self.temporary.name) / "staging"
        self.store = Store(FakeVFS(), str(self.root), str(self.staging))

    def tearDown(self):
        self.temporary.cleanup()

    def manifest(self, stamp="2026-09-06T140000Z", protected=False):
        return {"skin_id": "skin.example", "skin_version": "1.0", "device_id": "test-device",
                "device_name": "Test Device", "profile_id": "master", "created_at": stamp,
                "protected": protected}

    def valid_archive(self, label, stamp="2026-09-06T140000Z", protected=False):
        return build_archive(
            {"addon_data/skin.example/settings.xml":
             ("<settings><setting id=\"label\">{}</setting></settings>".format(label)).encode()},
            self.manifest(stamp, protected))

    def publish(self, blob=b"PK\x03\x04archive", stamp="2026-09-06T140000Z", protected=False):
        return self.store.publish(blob, self.manifest(stamp, protected))

    def test_publish_load_and_records_round_trip(self):
        blob = b"PK\x03\x04valid archive bytes"
        record = self.publish(blob)

        self.assertEqual(blob, self.store.load(record))
        self.assertEqual([record], self.store.records())
        self.assertEqual(record["sha256"], hashlib.sha256(blob).hexdigest())
        self.assertEqual(record, json.loads((self.root / (record["archive"] + ".json")).read_text()))

    def test_interrupted_or_malformed_markers_are_invisible(self):
        self.root.mkdir(parents=True)
        archive = "backup-20260906T140000-aabbccddeeff.zip"
        (self.root / archive).write_bytes(b"archive")
        (self.root / (archive + ".json")).write_text("{\"archive\":")
        missing = "backup-20260906T140001-001122334455.zip"
        (self.root / (missing + ".json")).write_text(json.dumps({"archive": missing}))
        (self.root / "backup-20260906T140002-001122334455.zip.json").write_text("[]")

        self.assertEqual([], self.store.records())

    def test_corrupt_archive_fails_read_back_verification(self):
        record = self.publish(b"original")
        (self.root / record["archive"]).write_bytes(b"corrupt")

        with self.assertRaises(BackupError):
            self.store.load(record)

    def test_failed_upload_never_prunes_earlier_committed_record(self):
        earlier = self.publish(b"earlier", "2026-09-06T140000Z")
        self.store.vfs.copy_allowed = False

        with self.assertRaises(BackupError):
            self.publish(b"new", "2026-09-06T140001Z")

        self.assertEqual([earlier], self.store.records())

    def test_protected_snapshots_survive_retention(self):
        protected = self.publish(self.valid_archive("protected", "2026-09-06T140000Z", True),
                                 "2026-09-06T140000Z", protected=True)
        ordinary_old = self.publish(self.valid_archive("old", "2026-09-06T140001Z"),
                                    "2026-09-06T140001Z", protected=False)
        ordinary_new = self.publish(self.valid_archive("new", "2026-09-06T140002Z"),
                                    "2026-09-06T140002Z", protected=False)

        self.store.prune(keep=1)

        self.assertEqual({protected["archive"], ordinary_new["archive"]},
                         {record["archive"] for record in self.store.records()})
        self.assertFalse((self.root / ordinary_old["archive"]).exists())

    def test_sidecar_protected_flag_tampering_cannot_expire_protected_archive(self):
        protected = self.publish(self.valid_archive("protected", protected=True), protected=False)
        marker_path = self.root / (protected["archive"] + ".json")
        marker = json.loads(marker_path.read_text())
        marker["manifest"]["protected"] = False
        marker_path.write_text(json.dumps(marker, sort_keys=True))

        self.store.prune(keep=0)

        self.assertTrue((self.root / protected["archive"]).exists())
        self.assertTrue(marker_path.exists())


if __name__ == "__main__":
    unittest.main()
