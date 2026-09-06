"""A device-local smoke test. All restore writes target a temporary fixture profile."""
import datetime
import copy
import json
import os
import platform
import tempfile
import time
import xml.etree.ElementTree as ET

import xbmc
import xbmcvfs

from resources.lib.archive import BackupError, build_archive, collect_files, read_archive, restore_files
from resources.lib.storage import Store


def check(condition, message):
    # Kodi can run Python with assertions disabled; never put test operations in assert.
    if not condition:
        raise BackupError(message)


def run(app):
    from resources.lib.runtime import atomic_json, operation_lock
    result = {'python': platform.python_version(), 'kodi': xbmc.getInfoLabel('System.BuildVersion'),
              'tests': [], 'success': False}
    start = time.monotonic()
    try:
        settings_path = os.path.join(xbmcvfs.translatePath(app.addon.getAddonInfo('path')), 'resources', 'settings.xml')
        strings = set()
        for element in ET.parse(settings_path).getroot().iter():
            for name in ('label', 'help'):
                value = element.get(name, '')
                if value.isdigit():
                    strings.add(int(value))
            if element.tag == 'heading' and (element.text or '').isdigit():
                strings.add(int(element.text))
        labels = {str(key): app.addon.getLocalizedString(key) for key in sorted(strings)}
        result['settings_labels'] = labels
        missing = [key for key, value in labels.items() if not value.strip()]
        check(not missing, 'Missing settings labels: ' + ', '.join(missing))
        result['tests'].append('Every settings label, help text and interval choice resolves in Kodi')
        check(app.addon.getSettingInt('interval_hours') in (1, 6, 12, 24, 168), 'Invalid interval setting')
        check(1 <= app.addon.getSettingInt('keep') <= 100, 'Invalid retention setting')
        result['tests'].append('Kodi settings schema and defaults')
        with operation_lock(app.lock_path):
            with tempfile.TemporaryDirectory(prefix='selftest-', dir=app.data) as root:
                skin = 'skin.backuptest'
                profile = os.path.join(root, 'profile')
                os.makedirs(os.path.join(profile, 'addon_data', skin))
                path = os.path.join(profile, 'addon_data', skin, 'settings.xml')
                with open(path, 'wb') as handle:
                    handle.write(b'<settings><setting id="test" type="string">Original</setting></settings>')
                files = collect_files(profile, skin)
                meta = {'skin_id': skin, 'skin_version': '1.0.0', 'device_id': 'selftest',
                        'device_name': 'Self-test', 'profile_id': 'fixture',
                        'created_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                        'protected': True}
                blob = build_archive(files, meta)
                manifest, decoded = read_archive(blob)
                check(decoded == files, 'Archive round trip mismatch')
                result['tests'].append('Archive round trip')
                store = Store(xbmcvfs, os.path.join(root, 'destination'), os.path.join(root, 'stage'))
                record = store.publish(blob, manifest)
                check(store.load(record) == blob and len(store.records()) == 1, 'VFS verification mismatch')
                result['tests'].append('Kodi VFS publish and read-back verification')
                with open(path, 'wb') as handle:
                    handle.write(b'<settings><setting id="test" type="string">Changed</setting></settings>')
                rollback = restore_files(profile, skin, decoded, os.path.join(root, 'rollback'))
                check(collect_files(profile, skin) == files, 'Restore content mismatch')
                check(os.path.isfile(os.path.join(rollback, 'transaction.json')), 'Missing restore journal')
                result['tests'].append('Transactional restore in temporary profile')
                actual_start = time.monotonic()
                actual = collect_files(app.profile, xbmc.getSkinDir())
                actual_meta = dict(meta, skin_id=xbmc.getSkinDir())
                actual_blob = build_archive(actual, actual_meta)
                check(read_archive(actual_blob)[1] == actual, 'Current skin round trip mismatch')
                result['actual_skin'] = {'skin': xbmc.getSkinDir(), 'files': len(actual),
                    'source_bytes': sum(map(len, actual.values())), 'archive_bytes': len(actual_blob),
                    'seconds': round(time.monotonic() - actual_start, 4)}
                result['tests'].append('Read-only backup of current skin')
            # Exercise orchestration with native Kodi APIs and an isolated destination/state.
            # Release the outer lock before using the same App method under a fixture lock.
        with tempfile.TemporaryDirectory(prefix='runtime-selftest-', dir=app.data) as root:
            class SettingsView:
                def getSetting(self, key):
                    return os.path.join(root, 'destination') if key == 'destination' else app.addon.getSetting(key)

                def getSettingInt(self, key):
                    return app.addon.getSettingInt(key)

            fixture = copy.copy(app)
            fixture.addon = SettingsView()
            fixture.data = root
            fixture.device_id = 'selftest'
            fixture.state_path = os.path.join(root, 'state.json')
            fixture.lock_path = os.path.join(root, 'operation.lock')
            fixture.rollback_root = os.path.join(root, 'rollback')
            fixture.pending_path = os.path.join(root, 'pending-restore.json')
            check(fixture.backup().startswith('Saved'), 'Native backup did not publish')
            check(fixture.backup().startswith('No changes'), 'Unchanged backup was not skipped')
            result['tests'].append('Native backup orchestration and unchanged-backup skipping')
        result['success'] = True
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        # Do not put settings or network credentials into diagnostic output.
        result['error'] = str(exc) if not isinstance(exc, OSError) else 'Filesystem operation failed'
    result['elapsed_seconds'] = round(time.monotonic() - start, 4)
    atomic_json(os.path.join(app.data, 'self-test-report.json'), result)
    xbmc.log('Skin Settings Backup self-test: ' + json.dumps(result), xbmc.LOGINFO)
    return result
