"""Kodi VFS transport. Publish a completion record only after read-back verification."""
import hashlib
import json
import os
import re
import tempfile
import uuid

from resources.lib.archive import BackupError, read_archive

MAX_ARCHIVE = 55 * 1024 * 1024
NAME = re.compile(r'^backup-\d{8}T\d{6}-[a-f0-9]{12}\.zip$')


def join(base, *parts):
    return base.rstrip('/\\') + '/' + '/'.join(parts)


class Store:
    def __init__(self, vfs, root, staging):
        self.vfs, self.root, self.staging = vfs, root, staging

    def read(self, path, limit=MAX_ARCHIVE):
        handle = self.vfs.File(path)
        try:
            size = handle.size()
            if size < 0 or size > limit:
                raise BackupError('File exceeds the backup size limit.')
            # Kodi allocates the requested buffer: do not allocate 55 MiB for an 8 KiB ZIP.
            data = bytes(handle.readBytes(size + 1))
            if len(data) > limit:
                raise BackupError('File exceeds the backup size limit.')
            if len(data) != size:
                raise BackupError('File changed or could not be read completely.')
            return data
        finally:
            handle.close()

    def records(self):
        if not self.vfs.exists(self.root + '/'):
            return []
        _, files = self.vfs.listdir(self.root)
        result = []
        for filename in files:
            if not filename.endswith('.zip.json') or not NAME.fullmatch(filename[:-5]):
                continue
            try:
                record = json.loads(self.read(join(self.root, filename), 1024 * 1024))
                if (not isinstance(record, dict) or record.get('archive') != filename[:-5] or
                        not isinstance(record.get('manifest'), dict) or
                        not re.fullmatch(r'[a-f0-9]{64}', record.get('sha256', '')) or
                        not isinstance(record.get('size'), int) or
                        not 0 < record['size'] <= MAX_ARCHIVE):
                    continue
                if self.vfs.exists(join(self.root, record['archive'])):
                    result.append(record)
            except (ValueError, TypeError, OSError, RuntimeError, BackupError):
                continue
        return sorted(result, key=lambda r: r['archive'], reverse=True)

    def load(self, record):
        name = record.get('archive', '')
        if not NAME.fullmatch(name):
            raise BackupError('Invalid backup filename.')
        blob = self.read(join(self.root, name))
        if len(blob) != record['size'] or hashlib.sha256(blob).hexdigest() != record['sha256']:
            raise BackupError('Backup verification failed. The archive is incomplete or damaged.')
        return blob

    def publish(self, blob, manifest):
        if len(blob) > MAX_ARCHIVE:
            raise BackupError('Archive exceeds the backup size limit.')
        if not self.vfs.mkdirs(self.root + '/') and not self.vfs.exists(self.root + '/'):
            raise BackupError('Cannot create the backup destination. Check share permissions.')
        stamp = re.sub(r'[^0-9T]', '', manifest['created_at'])[:15]
        name = 'backup-{}-{}.zip'.format(stamp, uuid.uuid4().hex[:12])
        if not NAME.fullmatch(name):
            raise BackupError('Invalid backup timestamp.')
        target = join(self.root, name)
        marker = target + '.json'
        record = {'archive': name, 'size': len(blob), 'sha256': hashlib.sha256(blob).hexdigest(),
                  'manifest': manifest}
        os.makedirs(self.staging, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix='upload-', suffix='.zip', dir=self.staging)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(blob)
            if not self.vfs.copy(path, target):
                raise BackupError('Cannot write the backup destination. Check share permissions.')
            self.load(record)
            payload = json.dumps(record, sort_keys=True).encode('utf-8')
            handle = self.vfs.File(marker, 'w')
            try:
                if not handle.write(payload):
                    raise BackupError('Cannot finish the backup record.')
            finally:
                handle.close()
            if self.read(marker, 1024 * 1024) != payload:
                raise BackupError('Backup record verification failed.')
            return record
        except Exception:
            self.vfs.delete(marker)
            self.vfs.delete(target)
            raise
        finally:
            os.unlink(path)

    def prune(self, keep):
        # Protected snapshots never expire. Only our committed archive names are removed.
        ordinary = [r for r in self.records() if not r['manifest'].get('protected', True)]
        for record in ordinary[max(1, keep):]:
            # A damaged sidecar must never turn a protected ZIP into an expiring one.
            try:
                manifest, _ = read_archive(self.load(record))
                if manifest.get('protected', True):
                    continue
            except (BackupError, OSError, RuntimeError):
                continue
            path = join(self.root, record['archive'])
            if self.vfs.delete(path + '.json'):
                self.vfs.delete(path)
