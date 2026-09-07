"""Kodi UI and service orchestration; archive contents stay deliberately small."""
import contextlib
import datetime
import hashlib
import json
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from resources.lib.archive import (BackupError, build_archive, collect_files, read_archive,
                                   restore_files, recover_pending, rollback_restore, validate_skin_id)
from resources.lib.policy import due, fingerprint, reset_reason, statistics
from resources.lib.storage import Store, join

ADDON_ID = 'service.skinsettings.backup'
TITLE = 'Skin Settings Backup'
AF3 = 'skin.arctic.fuse.3'
APPEARANCE = ('lookandfeel.skintheme', 'lookandfeel.skincolors', 'lookandfeel.font', 'lookandfeel.skinzoom')
PERSISTENCE_MARKER = 'service.skinsettings.backup.persist'
PERSISTENCE_VALUE = '1'
SKIN_SETTING_ID = re.compile(r'^[A-Za-z0-9_.-]{1,256}$')


class RestoredSettingsNotLoaded(BackupError):
    """The verified document is intact, but Kodi activated a stale live map."""


class RestoredSettingsDocumentChanged(BackupError):
    """The pending values are valid, but the staged on-disk document was replaced."""


def rpc(method, **params):
    answer = json.loads(xbmc.executeJSONRPC(json.dumps(
        {'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 1})))
    if 'error' in answer:
        raise BackupError('Kodi could not complete {}.'.format(method))
    return answer.get('result')


def atomic_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + '.' + uuid.uuid4().hex + '.tmp'
    try:
        with open(temp, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_bytes(path, data):
    """Durably replace a local file without exposing a partial XML document."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    if os.path.islink(directory) or os.path.islink(path):
        raise BackupError('Cannot safely persist skin settings through a symbolic link.')
    temp = path + '.' + uuid.uuid4().hex + '.tmp'
    try:
        with open(temp, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # Some Kodi platforms do not permit directory fsync after an otherwise valid replace.
            pass
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path, default=None):
    if not os.path.exists(path):
        return {} if default is None else default
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError('expected a JSON object')
        return value
    except (OSError, ValueError) as exc:
        raise BackupError('A local backup state file is damaged. Existing archives are unchanged.') from exc


def skin_setting_values(files, skin):
    """Decode the verified settings document into JSON-safe values for live application."""
    relative = 'addon_data/{}/settings.xml'.format(validate_skin_id(skin))
    try:
        root = ET.fromstring(files[relative])
    except (KeyError, ET.ParseError, ValueError) as exc:
        raise BackupError('The backup has no usable skin settings document.') from exc
    values, seen = [], set()
    for item in root.findall('setting'):
        setting_id = item.get('id') or item.get('name')
        if not setting_id or not SKIN_SETTING_ID.fullmatch(setting_id) or setting_id in seen:
            raise BackupError('The backup contains an invalid or duplicate skin setting id.')
        seen.add(setting_id)
        kind = item.get('type') or 'string'
        if kind == 'bool':
            value = (item.text or '').strip().lower() == 'true'
            json_type = 'boolean'
        elif kind == 'string':
            value = item.text or ''
            json_type = 'string'
        else:
            raise BackupError('The backup contains an unsupported skin setting type.')
        values.append({'id': setting_id, 'type': json_type, 'value': value})
    return values


def live_skin_setting_values(skin):
    """Read the active skin's authoritative in-memory settings through Kodi."""
    validate_skin_id(skin)
    result = rpc('Settings.GetSkinSettings')
    if not isinstance(result, dict) or result.get('skin') != skin or not isinstance(result.get('settings'), list):
        raise BackupError('Kodi did not expose the active skin settings.')
    values, seen = [], set()
    for item in result['settings']:
        if not isinstance(item, dict):
            raise BackupError('Kodi returned an invalid skin setting.')
        setting_id, kind, value = item.get('id'), item.get('type'), item.get('value')
        if (not isinstance(setting_id, str) or not SKIN_SETTING_ID.fullmatch(setting_id) or
                setting_id in seen or kind not in ('boolean', 'string') or
                (kind == 'boolean' and not isinstance(value, bool)) or
                (kind == 'string' and not isinstance(value, str))):
            raise BackupError('Kodi returned an invalid skin setting.')
        seen.add(setting_id)
        values.append({'id': setting_id, 'type': kind, 'value': value})
    return sorted(values, key=lambda item: item['id'].lower())


def skin_settings_document(values):
    """Encode a live setting snapshot in Kodi's portable skin settings format."""
    root = ET.Element('settings')
    for item in values:
        kind = 'bool' if item['type'] == 'boolean' else 'string'
        node = ET.SubElement(root, 'setting', {'id': item['id'], 'type': kind})
        node.text = ('true' if item['value'] else 'false') if kind == 'bool' else item['value']
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def skin_settings_equal(left, right):
    """Compare typed setting maps without depending on Kodi's XML serialization order."""
    if left is None or right is None or len(left) != len(right):
        return False
    return ({item['id']: (item['type'], item['value']) for item in left} ==
            {item['id']: (item['type'], item['value']) for item in right})


def checked_skin_setting_values(values, description='pending restore'):
    """Validate one complete typed setting map before it affects Kodi."""
    if not isinstance(values, list) or len(values) > 10000:
        raise BackupError('The {} contains invalid skin settings.'.format(description))
    checked, seen = [], set()
    for item in values:
        if (not isinstance(item, dict) or set(item) != {'id', 'type', 'value'} or
                not isinstance(item.get('id'), str) or not SKIN_SETTING_ID.fullmatch(item['id']) or
                item.get('type') not in ('boolean', 'string') or
                (item['type'] == 'boolean' and not isinstance(item.get('value'), bool)) or
                (item['type'] == 'string' and not isinstance(item.get('value'), str)) or
                item['id'] in seen):
            raise BackupError('The {} contains invalid or duplicate skin settings.'.format(description))
        seen.add(item['id'])
        checked.append(item)
    return checked


@contextlib.contextmanager
def operation_lock(path):
    """OS releases the lock after a crash; never guess an operation's expiry time."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if os.path.getsize(path) == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BackupError('Another backup or restore is already running.') from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BackupError('Another backup or restore is already running.') from exc
        yield
    finally:
        handle.close()


class App:
    def __init__(self):
        self.addon = xbmcaddon.Addon(ADDON_ID)
        self.profile = os.path.abspath(xbmcvfs.translatePath('special://profile/'))
        self.data = os.path.abspath(xbmcvfs.translatePath(self.addon.getAddonInfo('profile')))
        os.makedirs(self.data, exist_ok=True)
        self.state_path = os.path.join(self.data, 'state.json')
        self.lock_path = os.path.join(self.data, 'operation.lock')
        self.rollback_root = os.path.join(self.data, 'rollback')
        self.abandoned_root = os.path.join(self.data, 'abandoned')
        self.pending_path = os.path.join(self.data, 'pending-restore.json')
        self.monitor = xbmc.Monitor()
        self._progress = None
        self._progress_background = False
        self._progress_percent = 0
        # Identity creation is separate from state updates and serialized even on first launch.
        with operation_lock(os.path.join(self.data, 'identity.lock')):
            identity_path = os.path.join(self.data, 'identity.json')
            identity = read_json(identity_path)
            if not identity:
                identity = {'device_id': uuid.uuid4().hex}
                atomic_json(identity_path, identity)
            elif (set(identity) != {'device_id'} or not isinstance(identity.get('device_id'), str) or
                  not re.fullmatch(r'[A-Za-z0-9_.-]{8,128}', identity['device_id'])):
                raise BackupError('The local device identity is invalid. Keep identity.json for recovery and repair it before continuing.')
            self.device_id = identity['device_id']

    @contextlib.contextmanager
    def working(self, message):
        """Keep long interactive operations visibly active and always close the dialog."""
        if self._progress is not None:
            self.progress(self._progress_percent, message)
            yield
            return
        self._open_progress(message)
        try:
            yield
        finally:
            self._close_progress()

    def _open_progress(self, message):
        try:
            dialog = xbmcgui.DialogProgress()
            dialog.create(TITLE, message)
            background = False
        except Exception:
            try:
                dialog = xbmcgui.DialogProgressBG()
                dialog.create(TITLE, message)
                background = True
            except Exception:
                xbmc.log('{}: Progress dialog is unavailable.'.format(TITLE), xbmc.LOGWARNING)
                return
        self._progress = dialog
        self._progress_background = background
        self._progress_percent = 0

    def _open_background_progress(self, message):
        """Use a non-blocking indicator while Kodi owns the skin-confirmation dialog."""
        try:
            dialog = xbmcgui.DialogProgressBG()
            dialog.create(TITLE, message)
        except Exception:
            xbmc.log('{}: Background progress dialog is unavailable.'.format(TITLE), xbmc.LOGWARNING)
            return
        self._progress = dialog
        self._progress_background = True
        self._progress_percent = 0

    def instruction(self, message):
        """Show an instruction without stacking it over a modal progress dialog."""
        resume = self._progress is not None
        percent = self._progress_percent
        if resume:
            self._close_progress()
        accepted = xbmcgui.Dialog().ok(TITLE, message)
        if accepted is False:
            raise BackupError('Restore paused before the next skin change. Choose Finish restored skin to continue.')
        if resume:
            self._open_progress('Continuing restore')
            self.progress(percent, 'Continuing restore')

    def confirm(self, message):
        """Ask a consequential restore question without stacking modal windows."""
        resume = self._progress is not None
        percent = self._progress_percent
        if resume:
            self._close_progress()
        accepted = xbmcgui.Dialog().yesno(TITLE, message)
        if resume:
            self._open_progress('Continuing restore')
            self.progress(percent, 'Continuing restore')
        return accepted

    def _close_progress(self):
        dialog, self._progress = self._progress, None
        if dialog is not None:
            try:
                dialog.close()
            except Exception:
                pass
        self._progress_background = False
        self._progress_percent = 0

    def progress(self, percent, message):
        if self._progress is None:
            return
        self._progress_percent = max(0, min(100, int(percent)))
        try:
            if self._progress_background:
                self._progress.update(self._progress_percent, TITLE, message)
            else:
                self._progress.update(self._progress_percent, message)
        except Exception:
            # A skin switch can destroy its current progress window. Never let a
            # presentation failure interrupt a backup or leave a restore staged.
            self._close_progress()

    def pause(self, seconds):
        if self.monitor.waitForAbort(seconds):
            raise BackupError('Operation stopped because Kodi is closing.')
        if os.path.abspath(xbmcvfs.translatePath('special://profile/')) != self.profile:
            raise BackupError('Kodi profile changed. Run the operation again in the new profile.')

    def state(self):
        state = read_json(self.state_path)
        if not state:
            state['device_id'] = self.device_id
            state['skins'] = {}
        elif (not isinstance(state.get('device_id'), str) or
              not isinstance(state.get('skins'), dict) or
              any(not isinstance(key, str) or not isinstance(value, dict)
                  for key, value in state['skins'].items())):
            raise BackupError('The local backup state is invalid. Existing backups are unchanged; repair state.json before continuing.')
        return state

    def pending(self):
        pending = read_json(self.pending_path)
        if not pending:
            return pending
        skin = pending.get('skin_id')
        phase = pending.get('phase')
        paths = pending.get('paths', [])
        def unsafe_path(path):
            return (not isinstance(path, str) or not path or path.startswith(('/', '\\')) or
                    '..' in path.replace('\\', '/').split('/'))
        if (pending.get('schema_version') not in (None, 1) or
                not isinstance(skin, str) or phase not in ('restoring', 'rebuild', 'rollback_rebuild') or
                not isinstance(paths, list) or len(paths) > 2000 or
                any(unsafe_path(path) for path in paths)):
            raise BackupError('The pending restore state is invalid. It was preserved for recovery.')
        validate_skin_id(skin)
        if phase == 'rollback_rebuild' and ('skin_settings' not in pending or 'helper_hashes' not in pending):
            raise BackupError('The previous-settings verification state is incomplete. It was preserved for recovery.')
        if 'skin_settings' in pending:
            checked_skin_setting_values(pending['skin_settings'])
        hashes = pending.get('helper_hashes', {})
        if (not isinstance(hashes, dict) or any(
                unsafe_path(path) or not isinstance(digest, str) or
                not re.fullmatch(r'[0-9a-f]{64}', digest) for path, digest in hashes.items())):
            raise BackupError('The pending restore verification data is invalid. It was preserved for recovery.')
        for field in ('appearance', 'previous_appearance'):
            values = pending.get(field, {})
            if (not isinstance(values, dict) or any(
                    not isinstance(key, str) or len(key) > 256 or
                    not isinstance(value, (str, int, float, bool))
                    for key, value in values.items())):
                raise BackupError('The pending restore appearance data is invalid. It was preserved for recovery.')
        rollback = pending.get('rollback')
        if rollback is not None:
            if not isinstance(rollback, str):
                raise BackupError('The pending restore rollback location is invalid. It was preserved for recovery.')
            base = os.path.realpath(self.rollback_root)
            target = os.path.realpath(rollback)
            try:
                confined = os.path.commonpath((base, target)) == base and target != base
            except ValueError:
                confined = False
            if not confined:
                raise BackupError('The pending restore rollback location is outside this add-on. It was preserved for recovery.')
        return pending

    def destination(self):
        value = self.addon.getSetting('destination').strip()
        if not value:
            raise BackupError('Choose a backup destination first.')
        # Kodi's configured network sources can supply credentials without duplicating them.
        if re.match(r'^[a-zA-Z]+://[^/]*@', value):
            raise BackupError('Use a Kodi network source for passwords, then enter the share path without credentials.')
        return value

    def store(self, skin_id, state):
        validate_skin_id(skin_id)
        profile_id = hashlib.sha256(self.profile.encode()).hexdigest()[:12]
        root = join(self.destination(), 'SkinSettingsBackup', state['device_id'], profile_id, skin_id)
        return Store(xbmcvfs, root, os.path.join(self.data, 'staging'))

    def key(self, skin_id):
        return hashlib.sha256((self.destination().rstrip('/\\') + '\0' + skin_id).encode()).hexdigest()

    def incomplete(self):
        if not os.path.isdir(self.rollback_root):
            return False
        for name in os.listdir(self.rollback_root):
            path = os.path.join(self.rollback_root, name, 'transaction.json')
            if os.path.isfile(path):
                journal = read_json(path)
                if not isinstance(journal.get('profile_path'), str):
                    raise BackupError('A local rollback journal is invalid. It was preserved for recovery.')
                if journal.get('profile_path') != self.profile:
                    continue
                if (journal.get('version') != 1 or
                        journal.get('status') not in ('prepared', 'applying', 'rollback_failed',
                                                      'complete', 'rolled_back')):
                    raise BackupError('A local rollback journal is invalid. It was preserved for recovery.')
                if journal.get('status') in ('prepared', 'applying', 'rollback_failed'):
                    return True
        return False

    def appearance(self):
        values = {}
        for setting in APPEARANCE:
            try:
                result = rpc('Settings.GetSettingValue', setting=setting)
                if isinstance(result, dict) and result.get('value') is not None:
                    values[setting] = result['value']
            except BackupError:
                # A platform may not expose every interface setting.
                continue
        return values

    def skin_settings_path(self, skin):
        validate_skin_id(skin)
        return os.path.join(self.profile, 'addon_data', skin, 'settings.xml')

    def skin_settings_vfs_path(self, skin):
        return 'special://profile/addon_data/{}/settings.xml'.format(validate_skin_id(skin))

    @staticmethod
    def _read_vfs_bytes(path):
        handle = xbmcvfs.File(path)
        try:
            return bytes(handle.readBytes(handle.size()))
        finally:
            handle.close()

    @staticmethod
    def _write_vfs_bytes(path, data):
        handle = xbmcvfs.File(path, 'w')
        try:
            if handle.write(data) is False:
                raise OSError('VFS write returned false')
        finally:
            handle.close()

    def write_skin_settings_vfs(self, skin, values, document=None):
        """Write and verify settings through Kodi's VFS so tvOS updates its native store."""
        if xbmc.getSkinDir() == skin:
            raise BackupError('The target skin must be inactive while its settings are staged.')
        checked = checked_skin_setting_values(values)
        data = skin_settings_document(checked) if document is None else bytes(document)
        if not skin_settings_equal(
                skin_setting_values({'addon_data/{}/settings.xml'.format(skin): data}, skin), checked):
            raise BackupError('The staged settings document does not match its verified values.')
        directory = 'special://profile/addon_data/{}'.format(skin)
        if not xbmcvfs.mkdirs(directory) and not xbmcvfs.exists(directory):
            raise BackupError('Kodi could not create the skin settings folder.')
        path = self.skin_settings_vfs_path(skin)
        previous = None
        write_started = False
        try:
            if xbmcvfs.exists(path):
                previous = self._read_vfs_bytes(path)
            write_started = True
            self._write_vfs_bytes(path, data)
            saved = self._read_vfs_bytes(path)
            actual = skin_setting_values(
                {'addon_data/{}/settings.xml'.format(skin): saved}, skin)
            if not skin_settings_equal(actual, checked):
                raise BackupError('Kodi changed the restored skin settings while they were being staged.')
        except BackupError:
            try:
                if write_started and previous is not None:
                    self._write_vfs_bytes(path, previous)
                elif write_started and xbmcvfs.exists(path):
                    xbmcvfs.delete(path)
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                if write_started and previous is not None:
                    self._write_vfs_bytes(path, previous)
                elif write_started and xbmcvfs.exists(path):
                    xbmcvfs.delete(path)
            except Exception:
                pass
            raise BackupError('Kodi could not stage and verify the restored skin settings through its file manager.') from exc

    def skin_settings_valid(self, skin):
        path = self.skin_settings_path(skin)
        try:
            root = ET.parse(path).getroot()
            return root.tag == 'settings' and all(child.tag == 'setting' for child in root)
        except (OSError, ET.ParseError, ValueError):
            return False

    def saved_skin_setting_values(self, skin):
        path = self.skin_settings_path(skin)
        try:
            with open(path, 'rb') as handle:
                data = handle.read()
            return skin_setting_values(
                {'addon_data/{}/settings.xml'.format(skin): data}, skin)
        except (OSError, BackupError):
            return None

    def persist_skin_settings(self, skin, force=False):
        """Keep settings.xml equal to a stable snapshot of Kodi's live setting map."""
        validate_skin_id(skin)
        if skin != xbmc.getSkinDir():
            raise BackupError('The active skin changed before its settings could be saved.')
        expected = live_skin_setting_values(skin)
        if not force and skin_settings_equal(self.saved_skin_setting_values(skin), expected):
            return 'existing'

        # Create the profile directory before asking Kodi's deferred saver to use it.
        path = self.skin_settings_path(skin)
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        if os.path.islink(directory) or os.path.islink(path):
            raise BackupError('Cannot safely persist skin settings through a symbolic link.')
        xbmc.executebuiltin(
            'Skin.SetString({},{})'.format(PERSISTENCE_MARKER, PERSISTENCE_VALUE), True)

        # Kodi normally saves 500 ms later. Observe the result instead of assuming a fixed
        # delay was enough on a busy tvOS device.
        stable_reads = 0
        for attempt in range(40):
            self.pause(0.25)
            if skin != xbmc.getSkinDir():
                raise BackupError('The active skin changed while its settings were being saved.')
            current = live_skin_setting_values(skin)
            if skin_settings_equal(current, expected):
                stable_reads += 1
            else:
                expected, stable_reads = current, 0
            if skin_settings_equal(self.saved_skin_setting_values(skin), current):
                return 'native'
            # Give Kodi's saver five seconds. Only use the fallback after the live map
            # stayed unchanged across several reads, so an active edit is never overwritten.
            if attempt >= 19 and stable_reads >= 3:
                break
        else:
            raise BackupError('Skin settings kept changing. Wait for the skin to become idle and try again.')

        # The live JSON map is authoritative. A verified atomic copy both protects the
        # current device and gives Kodi a document to load on its next skin activation.
        atomic_bytes(path, skin_settings_document(expected))
        if (skin != xbmc.getSkinDir() or
                not skin_settings_equal(live_skin_setting_values(skin), expected) or
                not skin_settings_equal(self.saved_skin_setting_values(skin), expected)):
            raise BackupError('Kodi did not persist the skin settings and the recovery write failed.')
        xbmc.log('{}: repaired settings.xml from {} live settings for {}'.format(
            TITLE, len(expected), skin), xbmc.LOGWARNING)
        return 'repaired'

    def load_restored_skin_settings(self, skin, values):
        """Verify the document Kodi loaded when the restored skin was activated."""
        validate_skin_id(skin)
        if skin != xbmc.getSkinDir() or not isinstance(values, list):
            raise BackupError('Kodi is not ready to load the restored skin settings.')
        checked = checked_skin_setting_values(values)

        # restore_files replaced settings.xml while this skin was inactive, so activating
        # it loaded the complete document as one unit. ReloadSkin is deliberately avoided:
        # Kodi saves its current live state before reloading and could overwrite this file.
        if not skin_settings_equal(self.saved_skin_setting_values(skin), checked):
            raise RestoredSettingsDocumentChanged(
                'The restored skin settings document changed before Kodi could load it.')
        last_error = None
        for _attempt in range(20):
            if skin != xbmc.getSkinDir():
                raise BackupError('The active skin changed while restored settings were being verified.')
            try:
                self.verify_live_skin_settings(skin, checked)
                return
            except BackupError as exc:
                last_error = exc
                self.pause(0.25)
        raise RestoredSettingsNotLoaded(
            'Kodi did not load the complete restored settings document.') from last_error

    def verify_live_skin_settings(self, skin, values):
        live = rpc('Settings.GetSkinSettings')
        if not isinstance(live, dict) or live.get('skin') != skin or not isinstance(live.get('settings'), list):
            raise BackupError('Kodi did not expose the restored skin settings.')
        current = {item.get('id'): (item.get('type'), item.get('value'))
                   for item in live.get('settings', []) if isinstance(item, dict)}
        mismatched = [item['id'] for item in values
                      if current.get(item['id']) != (item['type'], item['value'])]
        if mismatched:
            raise BackupError('Kodi did not keep {} restored skin setting(s).'.format(len(mismatched)))

    def capture_files(self, skin):
        """Combine helper files with Kodi's live setting map, never a stale disk copy."""
        files = collect_files(self.profile, skin)
        values = live_skin_setting_values(skin)
        files['addon_data/{}/settings.xml'.format(skin)] = skin_settings_document(values)
        return files

    def backup(self, manual=False, protected=False):
        with operation_lock(self.lock_path):
            self.progress(5, 'Saving live skin settings')
            if os.path.exists(self.pending_path) or self.incomplete():
                raise BackupError('Finish or recover the pending restore before making another backup.')
            skin = xbmc.getSkinDir()
            validate_skin_id(skin)
            persistence = self.persist_skin_settings(skin, force=True)
            self.progress(25, 'Capturing skin settings and helper files')
            state = self.state()
            key = self.key(skin)
            previous = state['skins'].get(key, {})
            if previous.get('blocked') and not manual:
                raise BackupError(previous['blocked'])
            # Kodi delays saves. Read twice across a quiet period before trusting this snapshot.
            first = self.capture_files(skin)
            appearance = self.appearance()
            self.pause(2)
            self.progress(45, 'Verifying a stable snapshot')
            files = self.capture_files(skin)
            if (skin != xbmc.getSkinDir() or fingerprint(first) != fingerprint(files)
                    or appearance != self.appearance()):
                raise BackupError('Skin settings are still changing. Try again after the skin is idle.')
            stats = statistics(files, skin)
            reason = reset_reason(previous.get('stats'), stats)
            if reason:
                if not manual:
                    previous['blocked'] = reason + ' Automatic backups are paused to protect your earlier settings. Open the add-on and run a manual backup to review.'
                    state['skins'][key] = previous
                    atomic_json(self.state_path, state)
                    raise BackupError(previous['blocked'])
                if not xbmcgui.Dialog().yesno(TITLE, reason + '\nThis may be a reset. Save it as a separate protected snapshot?'):
                    return 'Backup cancelled.'
                protected = True
            store = self.store(skin, state)
            records = store.records()
            digest = fingerprint(dict(files, **{'appearance.json': json.dumps(appearance, sort_keys=True).encode()}))
            last_record = next((r for r in records if r['archive'] == previous.get('archive')), None)
            last_verified = False
            if not manual and digest == previous.get('fingerprint') and last_record:
                try:
                    store.load(last_record)
                    last_verified = True
                except (BackupError, OSError, RuntimeError):
                    pass
            if last_verified:
                previous['last_check'] = time.time()
                state['skins'][key] = previous
                atomic_json(self.state_path, state)
                return 'No changes since the last backup.'
            # The first snapshot is permanent, even after many subsequent automatic changes.
            protected = protected or not records
            self.progress(65, 'Building the backup archive')
            meta = {'skin_id': skin, 'skin_version': xbmcaddon.Addon(skin).getAddonInfo('version'),
                    'device_id': state['device_id'],
                    'device_name': self.addon.getSetting('device_name') or xbmc.getInfoLabel('System.FriendlyName') or 'Kodi',
                    'profile_id': hashlib.sha256(self.profile.encode()).hexdigest()[:12],
                    'created_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'protected': bool(protected), 'stats': stats, 'appearance': appearance}
            blob = build_archive(files, meta)
            manifest, _ = read_archive(blob)
            self.progress(85, 'Writing and verifying the backup')
            record = store.publish(blob, manifest)
            state['skins'][key] = {'last_check': time.time(), 'last_backup': meta['created_at'],
                                   'fingerprint': digest, 'stats': stats, 'archive': record['archive']}
            atomic_json(self.state_path, state)
            # Retention runs only after the new archive and completion record verified successfully.
            store.prune(max(1, self.addon.getSettingInt('keep')))
            message = 'Saved {} skin settings and {} files ({:.1f} KB){}.'.format(
                stats['settings'], len(files), len(blob) / 1024,
                ' as a protected snapshot' if protected else '')
            if persistence == 'repaired':
                message += ' Repaired settings.xml from Kodi’s live settings.'
            self.progress(100, 'Backup complete')
            return message

    def switch_skin(self, skin):
        if xbmc.getSkinDir() == skin:
            return
        resume_progress = self._progress is not None
        resume_percent = self._progress_percent
        if resume_progress:
            self._close_progress()
        try:
            xbmcgui.Dialog().notification(
                TITLE, 'Activating {}. Accept Kodi\u2019s Keep this skin prompt.'.format(skin),
                xbmcgui.NOTIFICATION_INFO, 12000)
        except Exception:
            pass
        self._open_background_progress('Waiting for Kodi to activate {}'.format(skin))
        try:
            xbmc.executebuiltin('ActivateWindow(home)')
            self.pause(0.5)
            if not rpc('Settings.SetSettingValue', setting='lookandfeel.skin', value=skin):
                raise BackupError('Kodi refused to change skins.')
            # Never put a modal progress window over Kodi's Keep this skin dialog. Once
            # that dialog closes, restore the modal immediately instead of waiting through
            # the old fixed eleven-second interval with no useful visual feedback.
            active_reads = 0
            confirmation_seen = False
            confirmed_reads = 0
            for _attempt in range(96):
                self.pause(0.25)
                if _attempt % 8 == 0:
                    self.progress(self._progress_percent, 'Waiting for Kodi to keep {}'.format(skin))
                if xbmc.getSkinDir() == skin:
                    active_reads += 1
                    confirmation_active = any(xbmc.getCondVisibility(condition) for condition in (
                        'Window.IsActive(yesnodialog)', 'Window.IsActive(10100)'))
                    if confirmation_active:
                        confirmation_seen = True
                        confirmed_reads = 0
                    elif confirmation_seen:
                        confirmed_reads += 1
                        if resume_progress and self._progress_background:
                            self._close_progress()
                            self._open_progress('Confirming {}'.format(skin))
                            self.progress(resume_percent, 'Confirming {}'.format(skin))
                        if confirmed_reads >= 4:
                            break
                    elif active_reads >= 44:
                        # Compatibility fallback for skins/platforms that do not expose
                        # DialogConfirm through Kodi's visibility conditions.
                        break
                else:
                    active_reads = 0
                    confirmed_reads = 0
            else:
                raise BackupError('Skin change was not kept. Accept Kodi’s “Keep this skin” prompt and try again.')
        except Exception:
            self._close_progress()
            raise
        if not resume_progress:
            self._close_progress()
        elif self._progress is None or self._progress_background:
            self._close_progress()
            self._open_progress('Continuing after activating {}'.format(skin))
            self.progress(resume_percent, 'Continuing after activating {}'.format(skin))

    def restore_blob(self, blob):
        manifest, files = read_archive(blob)
        skin = manifest['skin_id']
        restored_settings = skin_setting_values(files, skin)
        helper_files = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()
                        if path != 'addon_data/{}/settings.xml'.format(skin)}
        try:
            installed = xbmcaddon.Addon(skin)
        except RuntimeError as exc:
            raise BackupError('Install the backed-up skin and its dependencies before restoring.') from exc
        label = '{}\n{}\nDevice: {}\n{} skin settings; {} helper files'.format(
                skin, manifest['created_at'], manifest.get('device_name', 'Unknown'),
                len(restored_settings), len(helper_files))
        if not restored_settings:
            label += '\nWARNING: This backup contains no saved skin settings.'
        if installed.getAddonInfo('version') != manifest.get('skin_version'):
            label += '\nSkin version differs; older settings may behave differently.'
        label += '\nReplace this skin’s saved settings? A local rollback copy will be kept.'
        if not xbmcgui.Dialog().yesno('Restore skin settings', label):
            return
        with self.working('Preparing the restore'):
            with operation_lock(self.lock_path):
                self.progress(5, 'Checking restore safety')
                if self.incomplete() or os.path.exists(self.pending_path):
                    raise BackupError('Finish or recover the previous restore first.')
                if xbmc.Player().isPlaying():
                    raise BackupError('Stop playback before restoring.')
                previous_appearance = self.appearance() if xbmc.getSkinDir() == skin else {}
                if xbmc.getSkinDir() == skin:
                    fallback = 'skin.estuary' if skin != 'skin.estuary' else 'skin.estouchy'
                    try:
                        xbmcaddon.Addon(fallback)
                    except RuntimeError as exc:
                        raise BackupError('Switch to a different installed skin, then run Restore again.') from exc
                    self.instruction('Kodi will switch to {}. Accept “Keep this skin” so restoration can proceed.'.format(fallback))
                    self.progress(15, 'Activating a safe skin before writing')
                    self.switch_skin(fallback)
                if skin == xbmc.getSkinDir():
                    raise BackupError('The target skin is still active; restore stopped.')
                self.progress(30, 'Waiting for the target skin to close')
                self.pause(2)
                if skin == xbmc.getSkinDir():
                    raise BackupError('The target skin became active again; restore stopped before writing.')
                # Remember intent before mutation so a crash never looks like a completed operation.
                self.progress(40, 'Saving a local rollback point')
                pending = {'schema_version': 1, 'skin_id': skin, 'phase': 'restoring', 'created_at': manifest['created_at'],
                           'paths': list(files), 'started_at': time.time(),
                           'known_transactions': sorted(os.listdir(self.rollback_root)) if os.path.isdir(self.rollback_root) else [],
                           'appearance': manifest.get('appearance', {}),
                           'previous_appearance': previous_appearance,
                           'skin_settings': restored_settings, 'helper_hashes': helper_files}
                atomic_json(self.pending_path, pending)
                self.progress(55, 'Restoring skin settings and helper files')
                try:
                    rollback = restore_files(self.profile, skin, files, self.rollback_root)
                except Exception:
                    if not self.incomplete():
                        os.unlink(self.pending_path)
                    raise
                self.progress(65, 'Staging settings through Kodi’s file manager')
                self.write_skin_settings_vfs(
                    skin, restored_settings, files['addon_data/{}/settings.xml'.format(skin)])
                self.progress(75, 'Verifying staged restore files')
                pending.update({'phase': 'rebuild', 'rollback': rollback})
                journal = read_json(os.path.join(rollback, 'transaction.json'))
                pending['paths'] = sorted(set(pending['paths']) | {e['path'] for e in journal['previous_entries']})
                atomic_json(self.pending_path, pending)
                self.clear_helper_cache(skin, files)
                # Do not treat the previous configuration's reset baseline as a new reset after restore.
                state = self.state()
                state['skins'] = {}
                atomic_json(self.state_path, state)
                self.progress(100, 'Restore files staged safely')
        if xbmcgui.Dialog().ok(
                TITLE, 'Restore files are staged. Kodi will now activate {}. Accept “Keep this skin” '
                       'so verification can finish.'.format(skin)) is False:
            return
        with self.working('Activating the restored skin'):
            self.progress(5, 'Activating {}'.format(skin))
            self.switch_skin(skin)
            self.finish_restore(allow_restage=True)

    def clear_helper_cache(self, skin, files):
        win = xbmcgui.Window(10000)
        nodes = 'addon_data/script.skinvariables/nodes/'
        for path in files:
            if path.startswith(nodes):
                directory, filename = path[len(nodes):].rsplit('/', 1)
                win.clearProperty('SkinVariables.ShortcutsNode.{}-{}'.format(directory, filename))
        win.setProperty('SkinVariables.ShortcutsNode.Reload', str(time.time()))

    def finish_restore(self, allow_restage=False):
        with operation_lock(self.lock_path):
            self.progress(10, 'Checking the staged restore')
            pending = self.pending()
            if not pending:
                return
            if pending.get('phase') not in ('rebuild', 'rollback_rebuild') or self.incomplete():
                raise BackupError('A restore was interrupted. Choose Recover interrupted restore.')
            skin = pending['skin_id']
            if xbmc.getSkinDir() != skin:
                raise BackupError('Activate {} and accept the skin change, then choose Finish restored skin.'.format(skin))
            if 'skin_settings' in pending:
                self.progress(25, 'Loading restored skin settings')
                try:
                    self.load_restored_skin_settings(skin, pending['skin_settings'])
                except (RestoredSettingsNotLoaded, RestoredSettingsDocumentChanged) as exc:
                    if not allow_restage:
                        raise BackupError(
                            'Kodi has not loaded the complete pending restore. Open Skin Settings Backup '
                            'and choose Finish restored skin to continue safely.') from exc
                    if (isinstance(exc, RestoredSettingsDocumentChanged) and not self.confirm(
                            'Kodi replaced the staged settings document after activation. Restage the '
                            'verified pending backup and try the skin activation again?')):
                        raise BackupError(
                            'Restore remains pending. Choose Finish restored skin when ready.') from exc
                    self.restage_and_reactivate(pending)
                    self.load_restored_skin_settings(skin, pending['skin_settings'])
            self.progress(40, 'Refreshing restored helper data')
            self.clear_helper_cache(skin, pending.get('paths', []))
            for setting in APPEARANCE:
                if setting not in pending.get('appearance', {}):
                    continue
                value = pending['appearance'][setting]
                current = rpc('Settings.GetSettingValue', setting=setting).get('value')
                if current != value and not rpc('Settings.SetSettingValue', setting=setting, value=value):
                    raise BackupError('Kodi could not apply the restored appearance. Choose a supported theme/font and retry Finish restored skin.')
            if skin == AF3:
                self.progress(60, 'Rebuilding Arctic Fuse 3 menus and widgets')
                self.rebuild_af3(pending)
                self.pause(2)
            self.progress(90, 'Verifying the completed restore')
            if 'skin_settings' in pending:
                self.verify_live_skin_settings(skin, pending['skin_settings'])
            self.verify_restored_helpers(pending)
            state = self.state()
            state['skins'] = {}
            atomic_json(self.state_path, state)
            os.unlink(self.pending_path)
            self.progress(100, 'Restore complete')
            message = ('Previous settings restored and verified.' if pending.get('phase') == 'rollback_rebuild'
                       else 'Restore complete: {} skin settings; {} helper files.'.format(
                           len(pending.get('skin_settings', [])), len(pending.get('helper_hashes', {}))))
            xbmcgui.Dialog().notification(TITLE, message, xbmcgui.NOTIFICATION_INFO)

    def restage_and_reactivate(self, pending):
        """Recover a valid transaction that tvOS did not load into the skin cache."""
        skin = pending['skin_id']
        fallback = 'skin.estuary' if skin != 'skin.estuary' else 'skin.estouchy'
        try:
            xbmcaddon.Addon(fallback)
        except RuntimeError as exc:
            raise BackupError('Install or activate a different skin before retrying this restore.') from exc
        self.instruction(
            'Kodi did not load every staged setting. It will safely switch to {}, restage the same '
            'verified settings through Kodi’s file manager, and reactivate {}. Accept each '
            '“Keep this skin” prompt.'.format(fallback, skin))
        self.progress(28, 'Activating a safe skin for recovery')
        self.switch_skin(fallback)
        if xbmc.getSkinDir() == skin:
            raise BackupError('The target skin remained active; no settings were written.')
        self.progress(32, 'Restaging the verified settings through Kodi')
        self.write_skin_settings_vfs(skin, pending['skin_settings'])
        self.instruction(
            'The verified settings are staged. Kodi will now reactivate {}. Accept '
            '“Keep this skin” to continue.'.format(skin))
        self.progress(36, 'Reactivating the restored skin')
        self.switch_skin(skin)

    def verify_restored_helpers(self, pending):
        """Do not report success if AF3 or its helper replaced restored source data."""
        expected = pending.get('helper_hashes', {})
        if not isinstance(expected, dict):
            raise BackupError('The pending restore contains invalid helper verification data.')
        mismatched = []
        for relative, digest in expected.items():
            if (not isinstance(relative, str) or not isinstance(digest, str) or
                    not re.fullmatch(r'[0-9a-f]{64}', digest)):
                raise BackupError('The pending restore contains invalid helper verification data.')
            path = os.path.join(self.profile, *relative.split('/'))
            try:
                with open(path, 'rb') as handle:
                    actual = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                actual = None
            if actual != digest:
                mismatched.append(relative)
        if mismatched:
            raise BackupError('{} restored helper file(s) did not remain applied.'.format(len(mismatched)))

    def rebuild_af3(self, pending):
        if not xbmc.getCondVisibility('System.AddonIsEnabled(script.skinvariables)'):
            raise BackupError('Enable Skin Variables, then choose Finish restored skin.')
        # AF3 has one generated include pointing to the selected skin profile.
        slug = xbmc.getInfoLabel('Skin.String(SkinVariables.SkinUser)')
        if slug and not re.fullmatch(r'user-[A-Za-z0-9]+', slug):
            raise BackupError('Unrecognized AF3 skin profile. Use AF3’s profile picker, then finish restore.')
        skin_path = xbmcvfs.translatePath(xbmcaddon.Addon(AF3).getAddonInfo('path'))
        if slug:
            target = os.path.join(skin_path, '1080i', 'script-skinvariables-skinusers.xml')
            root = ET.Element('includes')
            ET.SubElement(root, 'include', {'file': 'script-skinvariables-generator-includes-{}.xml'.format(slug)})
            content = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            atomic_bytes(target, content)
        # Run the helper's public script routes in a single invocation, synchronously in its interpreter.
        token = uuid.uuid4().hex
        prop = 'SkinSettingsBackup.RebuildComplete'
        xbmcgui.Window(10000).clearProperty(prop)
        actions = ['route=template=images&force=True&no_reload=True',
                   'route=template=labels&force=True&no_reload=True',
                   'route=force=True&no_reload=True',
                   'route=action=buildviews&force=True&no_reload=True',
                   'route=action=buildtemplate&force=True&no_reload=True',
                   'SetProperty({},{},Home)'.format(prop, token)]
        plan_path = os.path.join(self.data, 'rebuild.json')
        atomic_json(plan_path, {'actions': actions})
        # RunAddon/JSON-RPC ExecuteAddon chooses this helper's plugin entrypoint, not its script.
        # Use a fixed special:// path so quoting cannot turn a filesystem path into builtin arguments.
        xbmc.executebuiltin('RunScript(script.skinvariables,run_executebuiltin=special://profile/addon_data/'
                            + ADDON_ID + '/rebuild.json,use_rules=True)')
        deadline = time.monotonic() + 60
        while xbmcgui.Window(10000).getProperty(prop) != token:
            if xbmc.getSkinDir() != AF3 or time.monotonic() > deadline:
                raise BackupError('AF3 rebuild did not finish. Check Kodi’s log and choose Finish restored skin to retry.')
            self.pause(0.25)
            self.progress(70, 'Waiting for Arctic Fuse 3 menus and widgets')
        xbmcgui.Window(10000).clearProperty(prop)
        generated = [os.path.join(skin_path, '1080i', name) for name in (
            'script-skinvariables-includes.xml',
            'script-skinvariables-labels-includes.xml',
            'script-skinvariables-images-includes.xml')]
        for path in generated:
            try:
                if ET.parse(path).getroot().tag != 'includes':
                    raise ValueError('unexpected root')
            except (OSError, ET.ParseError, ValueError) as exc:
                raise BackupError('AF3 menu generation was incomplete. Choose Finish restored skin to retry.') from exc
        if 'skin_settings' in pending:
            self.verify_live_skin_settings(AF3, pending['skin_settings'])
        # AF3 reads the newly generated include files during a reload. The imported live
        # settings are verified immediately before and again by finish_restore afterward.
        xbmc.executebuiltin('ReloadSkin()', True)
        if xbmc.getSkinDir() != AF3:
            raise BackupError('AF3 did not remain active after its menus were reloaded.')
        for path in generated:
            try:
                if ET.parse(path).getroot().tag != 'includes':
                    raise ValueError('unexpected root')
            except (OSError, ET.ParseError, ValueError) as exc:
                raise BackupError('AF3 generated menu files changed during reload. Choose Finish restored skin to retry.') from exc

    def recovery(self):
        with operation_lock(self.lock_path):
            pending = self.pending()
            if pending and xbmc.getSkinDir() == pending.get('skin_id'):
                raise BackupError('Switch to a different skin before recovering interrupted writes.')
            if pending and pending.get('phase') in ('rebuild', 'rollback_rebuild'):
                message = ('Previous files are back in place. Activate the skin and choose Finish restoring previous settings.'
                           if pending.get('phase') == 'rollback_rebuild' else
                           'File restoration is complete. Activate the restored skin and choose Finish restored skin, or choose Restore previous settings to cancel it.')
                xbmcgui.Dialog().ok(TITLE, message)
                return
            if not xbmcgui.Dialog().yesno(TITLE, 'Undo any interrupted file writes using the saved local rollback copies?'):
                return
            with self.working('Recovering interrupted restore files'):
                self.progress(20, 'Checking rollback journals')
                recovered = recover_pending(self.profile, self.rollback_root)
                self.progress(75, 'Verifying recovered files')
            if pending and pending.get('phase') == 'restoring':
                if recovered:
                    with self.working('Finalizing recovered settings'):
                        self.progress(85, 'Restaging the recovered previous settings through Kodi')
                        files = collect_files(self.profile, pending['skin_id'])
                        settings = skin_setting_values(files, pending['skin_id'])
                        document = files.get(
                            'addon_data/{}/settings.xml'.format(pending['skin_id']),
                            skin_settings_document(settings))
                        self.write_skin_settings_vfs(pending['skin_id'], settings, document)
                    os.unlink(self.pending_path)
                    xbmcgui.Dialog().ok(TITLE, 'Recovered {} interrupted transaction(s). Previous files are back in place.'.format(len(recovered)))
                    return
                completed = []
                if not recovered and os.path.isdir(self.rollback_root):
                    for name in os.listdir(self.rollback_root):
                        path = os.path.join(self.rollback_root, name, 'transaction.json')
                        new_transaction = (name not in pending.get('known_transactions', [])
                                           if 'known_transactions' in pending else
                                           os.path.isfile(path) and os.path.getmtime(path) >= pending.get('started_at', float('inf')))
                        if os.path.isfile(path) and new_transaction:
                            journal = read_json(path)
                            if (journal.get('status') == 'complete' and journal.get('skin_id') == pending['skin_id']
                                    and journal.get('profile_path') == self.profile):
                                completed.append((path, journal))
                if completed:
                    path, journal = max(completed, key=lambda item: os.path.getmtime(item[0]))
                    if 'skin_settings' in pending:
                        with self.working('Finalizing restored settings'):
                            self.progress(85, 'Staging settings through Kodi’s file manager')
                            self.write_skin_settings_vfs(
                                pending['skin_id'], pending['skin_settings'])
                    pending.update({'phase': 'rebuild', 'rollback': os.path.dirname(path),
                                    'paths': sorted(set(pending.get('paths', [])) | {e['path'] for e in journal['previous_entries']})})
                    atomic_json(self.pending_path, pending)
                    xbmcgui.Dialog().ok(TITLE, 'The files had already finished restoring. Activate the restored skin, then choose Finish restored skin.')
                    return
                raise BackupError('Recovery could not identify the restore transaction. Pending state was preserved; do not make a new backup until the rollback journal is repaired.')
            xbmcgui.Dialog().ok(TITLE, 'Recovered {} interrupted transaction(s).'.format(len(recovered)))

    def cancel_restore(self):
        """Explicitly restore the exact pre-restore snapshot retained by this transaction."""
        with operation_lock(self.lock_path):
            pending = self.pending()
            if not pending or pending.get('phase') != 'rebuild' or not pending.get('rollback'):
                raise BackupError('There is no completed staged restore to cancel.')
            skin = pending['skin_id']
            if xbmc.getSkinDir() == skin:
                raise BackupError('Switch to a different skin before restoring the previous settings.')
            if not xbmcgui.Dialog().yesno(
                    TITLE, 'Cancel this staged restore and put back the settings and helper files saved immediately before it?'):
                return
            with self.working('Restoring the previous skin state'):
                self.progress(20, 'Applying the local rollback copy')
                rollback_restore(self.profile, pending['rollback'], expected_skin_id=skin)
                self.progress(55, 'Reading the restored previous settings')
                files = collect_files(self.profile, skin)
                settings = skin_setting_values(files, skin)
                self.progress(65, 'Restaging previous settings through Kodi')
                settings_document = files.get(
                    'addon_data/{}/settings.xml'.format(skin), skin_settings_document(settings))
                self.write_skin_settings_vfs(skin, settings, settings_document)
                helper_hashes = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()
                                 if path != 'addon_data/{}/settings.xml'.format(skin)}
                self.progress(75, 'Saving the rollback completion state')
                pending.update({'paths': list(files),
                                'phase': 'rollback_rebuild',
                                'skin_settings': settings, 'helper_hashes': helper_hashes,
                                'appearance': pending.get('previous_appearance', {})})
                atomic_json(self.pending_path, pending)
                self.clear_helper_cache(skin, files)
                state = self.state()
                state['skins'] = {}
                atomic_json(self.state_path, state)
                self.progress(100, 'Previous files restored')
        xbmcgui.Dialog().ok(TITLE, 'Previous files restored. Activate {}, then choose Finish restoring previous settings so menus can be rebuilt and verified.'.format(skin))

    def abandon_restore(self, reason='User abandoned a stuck restore.'):
        """Unlock the add-on without changing any live skin or helper file."""
        with operation_lock(self.lock_path):
            if not os.path.exists(self.pending_path):
                raise BackupError('There is no pending restore to abandon.')
            pending = read_json(self.pending_path)
            os.makedirs(self.abandoned_root, exist_ok=True)
            name = '{}-{}.json'.format(
                datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                uuid.uuid4().hex[:8])
            atomic_json(os.path.join(self.abandoned_root, name), {
                'archived_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'reason': reason,
                'pending': pending,
            })
            os.unlink(self.pending_path)
            return True

    def release_impossible_rollback(self):
        """Quarantine a rollback that can never match Kodi's non-empty live state."""
        pending = self.pending()
        if (not pending or pending.get('phase') != 'rollback_rebuild' or
                pending.get('skin_settings') != [] or xbmc.getSkinDir() != pending.get('skin_id')):
            return False
        if not live_skin_setting_values(pending['skin_id']):
            return False
        return self.abandon_restore(
            'Rollback expected an empty skin configuration but Kodi has live settings; live files were kept unchanged.')

    def choose_restore(self):
        # Local device's backups are quick to list. Import handles another device or saved ZIP.
        state = self.state()
        skin = xbmc.getSkinDir()
        candidates = set([skin])
        result = rpc('Addons.GetAddons', type='xbmc.gui.skin', properties=['name'])
        candidates.update(item['addonid'] for item in result.get('addons', []))
        choices = []
        for candidate in sorted(candidates):
            store = self.store(candidate, state)
            for record in store.records():
                meta = record['manifest']
                if meta.get('skin_id') != candidate:
                    continue
                label = '{} | {}{}'.format(meta.get('created_at', ''), candidate,
                        ' | Protected' if meta.get('protected') else '')
                choices.append((label, store, record))
        choices.sort(key=lambda item: item[0], reverse=True)
        if not choices:
            xbmcgui.Dialog().ok(TITLE, 'No completed backups for this device and profile. Use Import backup ZIP for a backup from another device.')
            return
        selected = xbmcgui.Dialog().select('Restore point', [item[0] for item in choices])
        if selected >= 0:
            _, store, record = choices[selected]
            with self.working('Reading and verifying the selected backup'):
                blob = store.load(record)
            self.restore_blob(blob)

    def status(self):
        state = self.state()
        entries = [value.get('last_backup', 'None') for value in state['skins'].values()]
        text = 'Last successful backup: {}\nDevice ID: {}\nPending restore: {}\n\n'.format(
            max(entries) if entries else 'None', state['device_id'], os.path.exists(self.pending_path))
        blocked = [value['blocked'] for value in state['skins'].values() if value.get('blocked')]
        if blocked:
            text += '\n'.join(blocked) + '\n\n'
        text += ('Includes: main skin settings; Skin Variables menus, widgets, views and skin profiles.\n'
                 'Includes Kodi’s theme, color, font and zoom selections. Other skin helper add-ons, artwork, '
                 'media databases and add-on account settings are outside this backup.\n\n'
                 'Automatic backups run when Kodi is awake and idle. Unchanged settings are skipped. '
                 'First and protected snapshots never expire. Backups are not encrypted.')
        xbmcgui.Dialog().textviewer(TITLE, text)


def run_ui(args=None):
    try:
        app = App()
        if args == ['--self-test']:
            from resources.lib.diagnostics import run
            with app.working('Running add-on self-test'):
                run(app)
            return
        actions = [('backup', 'Back up now'), ('protect', 'Save protected snapshot'),
                   ('restore', 'Restore backup'), ('import', 'Import backup ZIP'),
                   ('destination', 'Choose destination'), ('settings', 'Settings'),
                   ('status', 'Instructions / Readme')]
        try:
            pending = app.pending()
        except BackupError:
            pending = None
        if pending and pending.get('phase') in ('rebuild', 'rollback_rebuild'):
            actions.append(('finish', 'Finish restoring previous settings' if pending.get('phase') == 'rollback_rebuild'
                            else 'Finish restored skin'))
            if pending.get('phase') == 'rebuild':
                actions.extend([
                            ('cancel', 'Restore previous settings (cancel staged restore)')])
            actions.append(('abandon', 'Abandon stuck restore (keep current skin)'))
        elif (pending and pending.get('phase') == 'restoring') or app.incomplete():
            actions.append(('recover', 'Recover interrupted restore'))
        if os.path.exists(app.pending_path) and not any(action == 'abandon' for action, _label in actions):
            actions.append(('abandon', 'Abandon stuck restore (keep current skin)'))
        actions.append(('selftest', 'Run self-test'))
        selected = xbmcgui.Dialog().select(TITLE, [label for _action, label in actions])
        if selected < 0:
            return
        action = actions[selected][0]
        if action in ('backup', 'protect'):
            with app.working('Preparing a skin settings backup'):
                message = app.backup(manual=True, protected=action == 'protect')
            xbmcgui.Dialog().ok(TITLE, message)
        elif action == 'restore':
            app.choose_restore()
        elif action == 'import':
            path = xbmcgui.Dialog().browseSingle(1, 'Choose skin backup ZIP', 'files', '.zip')
            if path:
                with app.working('Reading and verifying the backup ZIP'):
                    blob = Store(xbmcvfs, '', os.path.join(app.data, 'staging')).read(path)
                app.restore_blob(blob)
        elif action == 'destination':
            path = xbmcgui.Dialog().browseSingle(0, 'Choose backup folder', 'files', treatAsFolder=True)
            if path:
                app.addon.setSetting('destination', path)
        elif action == 'settings':
            app.addon.openSettings()
        elif action == 'status':
            app.status()
        elif action == 'finish':
            with app.working('Finishing and verifying the restore'):
                app.finish_restore(allow_restage=True)
        elif action == 'cancel':
            app.cancel_restore()
        elif action == 'abandon':
            if xbmcgui.Dialog().yesno(
                    TITLE, 'Keep the current skin and helper files exactly as they are, archive the stuck transaction, and allow another restore?'):
                app.abandon_restore()
                xbmcgui.Dialog().ok(TITLE, 'The stuck transaction was archived. Current Kodi and skin files were not changed.')
        elif action == 'recover':
            app.recovery()
        elif action == 'selftest':
            from resources.lib.diagnostics import run
            with app.working('Running add-on self-test'):
                result = run(app)
            summary = '{}\n{} of {} checks passed\nKodi {} | Python {}\nElapsed: {} seconds'.format(
                'Self-test passed' if result.get('success') else 'Self-test failed',
                len(result.get('tests', [])), result.get('total_checks', len(result.get('tests', []))),
                result.get('kodi', 'Unknown'), result.get('python', 'Unknown'), result.get('elapsed_seconds', 'Unknown'))
            if result.get('error'):
                summary += '\n\n' + result['error']
            xbmcgui.Dialog().textviewer(TITLE, summary)
    except Exception as exc:
        report_error(exc, interactive=True)


def report_error(exc, interactive=False):
    # Unexpected exceptions may include passwords or private paths. Keep Kodi's log concise.
    message = str(exc) if isinstance(exc, BackupError) else 'Operation failed ({}). Existing completed backups are retained.'.format(type(exc).__name__)
    xbmc.log('{}: {}'.format(TITLE, message), xbmc.LOGWARNING)
    if interactive:
        xbmcgui.Dialog().ok(TITLE, message)
    return message


def run_service():
    monitor = xbmc.Monitor()
    if monitor.waitForAbort(45):
        return
    last_notice, last_error, retry_after, next_guard = 0, '', 0, 0
    while not monitor.abortRequested():
        try:
            app = App()
            now = time.time()
            idle = not xbmc.Player().isPlaying() and xbmc.getGlobalIdleTime() >= 15
            if idle and now >= retry_after:
                if os.path.exists(app.pending_path):
                    pending = app.pending()
                    if app.release_impossible_rollback():
                        xbmc.log('{}: quarantined an impossible empty rollback; live files were unchanged.'.format(TITLE), xbmc.LOGWARNING)
                        last_error = ''
                        continue
                    if (pending.get('phase') in ('rebuild', 'rollback_rebuild') and
                            xbmc.getSkinDir() == pending.get('skin_id')):
                        app.finish_restore()
                        last_error = ''
                    elif pending.get('phase') == 'restoring':
                        raise BackupError('A restore was interrupted. Open Skin Settings Backup and choose Recover interrupted restore.')
                else:
                    if now >= next_guard:
                        try:
                            with operation_lock(app.lock_path):
                                app.persist_skin_settings(xbmc.getSkinDir())
                        except BackupError as exc:
                            if str(exc) != 'Another backup or restore is already running.':
                                raise
                    next_guard = now + 300
                if (not os.path.exists(app.pending_path) and app.addon.getSettingBool('enabled')
                        and app.addon.getSetting('destination')):
                    state = app.state()
                    previous = state['skins'].get(app.key(xbmc.getSkinDir()), {})
                    interval = app.addon.getSettingInt('interval_hours') or 24
                    if due(previous.get('last_check'), now, interval):
                        message = app.backup()
                        last_error = ''
                        if app.addon.getSettingBool('notify_success') and message.startswith('Saved'):
                            xbmcgui.Dialog().notification(TITLE, message, xbmcgui.NOTIFICATION_INFO)
        except Exception as exc:
            message = report_error(exc)
            retry_after = time.time() + 300
            if message != last_error or time.time() - last_notice >= 86400:
                xbmcgui.Dialog().notification(TITLE, message, xbmcgui.NOTIFICATION_WARNING, 7000)
                last_error, last_notice = message, time.time()
        if monitor.waitForAbort(30):
            break
