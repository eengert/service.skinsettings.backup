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
                                   restore_files, recover_pending, validate_skin_id)
from resources.lib.policy import due, fingerprint, reset_reason, statistics
from resources.lib.storage import Store, join

ADDON_ID = 'service.skinsettings.backup'
TITLE = 'Skin Settings Backup'
AF3 = 'skin.arctic.fuse.3'
APPEARANCE = ('lookandfeel.skintheme', 'lookandfeel.skincolors', 'lookandfeel.font', 'lookandfeel.skinzoom')
PERSISTENCE_MARKER = 'service.skinsettings.backup.persist'
PERSISTENCE_VALUE = '1'
SKIN_SETTING_ID = re.compile(r'^[A-Za-z0-9_.-]{1,256}$')


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


def read_json(path, default=None):
    if not os.path.exists(path):
        return {} if default is None else default
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
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
        self.pending_path = os.path.join(self.data, 'pending-restore.json')
        self.monitor = xbmc.Monitor()
        # Identity creation is separate from state updates and serialized even on first launch.
        with operation_lock(os.path.join(self.data, 'identity.lock')):
            identity_path = os.path.join(self.data, 'identity.json')
            identity = read_json(identity_path)
            if not identity:
                identity = {'device_id': uuid.uuid4().hex}
                atomic_json(identity_path, identity)
            self.device_id = identity['device_id']

    def pause(self, seconds):
        if self.monitor.waitForAbort(seconds):
            raise BackupError('Operation stopped because Kodi is closing.')
        if os.path.abspath(xbmcvfs.translatePath('special://profile/')) != self.profile:
            raise BackupError('Kodi profile changed. Run the operation again in the new profile.')

    def state(self):
        state = read_json(self.state_path)
        if not state.get('device_id'):
            state['device_id'] = self.device_id
            state['skins'] = {}
        return state

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
            if os.path.isfile(path) and read_json(path).get('status') in (
                    'prepared', 'applying', 'rollback_failed'):
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

    def skin_settings_valid(self, skin):
        path = self.skin_settings_path(skin)
        try:
            root = ET.parse(path).getroot()
            return root.tag == 'settings' and all(child.tag == 'setting' for child in root)
        except (OSError, ET.ParseError, ValueError):
            return False

    def persist_skin_settings(self, skin, force=False):
        """Ask Kodi to write the active skin's in-memory settings through its native saver."""
        validate_skin_id(skin)
        if skin != xbmc.getSkinDir():
            raise BackupError('The active skin changed before its settings could be saved.')
        if not force and self.skin_settings_valid(skin):
            return True
        xbmc.executebuiltin('Skin.SetString({},{})'.format(PERSISTENCE_MARKER, PERSISTENCE_VALUE))
        # Kodi's skin settings saver is intentionally deferred by 500 ms.
        self.pause(0.75)
        if skin != xbmc.getSkinDir():
            raise BackupError('The active skin changed while its settings were being saved.')
        if not self.skin_settings_valid(skin):
            xbmc.log('{}: Kodi did not persist settings.xml for {}'.format(TITLE, skin), xbmc.LOGWARNING)
            return False
        return True

    def apply_skin_settings(self, skin, values):
        """Apply archived values to Kodi's active skin and verify its live setting map."""
        validate_skin_id(skin)
        if skin != xbmc.getSkinDir() or not isinstance(values, list):
            raise BackupError('Kodi is not ready to apply the restored skin settings.')
        checked = []
        for item in values:
            if (not isinstance(item, dict) or set(item) != {'id', 'type', 'value'} or
                    not isinstance(item['id'], str) or not SKIN_SETTING_ID.fullmatch(item['id']) or
                    item['type'] not in ('boolean', 'string') or
                    (item['type'] == 'boolean' and not isinstance(item['value'], bool)) or
                    (item['type'] == 'string' and not isinstance(item['value'], str))):
                raise BackupError('The pending restore contains invalid skin settings.')
            checked.append(item)
        if len({item['id'] for item in checked}) != len(checked):
            raise BackupError('The pending restore contains duplicate skin settings.')

        live = rpc('Settings.GetSkinSettings')
        if not isinstance(live, dict) or live.get('skin') != skin or not isinstance(live.get('settings'), list):
            raise BackupError('Kodi did not expose the active skin settings.')
        available = {item.get('id'): item.get('type') for item in live['settings'] if isinstance(item, dict)}
        for item in checked:
            if item['id'] in available:
                continue
            command = 'Skin.SetBool({})' if item['type'] == 'boolean' else 'Skin.SetString({},1)'
            xbmc.executebuiltin(command.format(item['id']))

        # Reset values that exist only on the target device, then apply the complete source set.
        xbmc.executebuiltin('Skin.ResetSettings')
        for item in checked:
            rpc('Settings.SetSkinSettingValue', setting=item['id'], value=item['value'])
        if not self.persist_skin_settings(skin, force=True):
            raise BackupError('Kodi applied the restored settings in memory but could not persist them.')

        # Reload from the just-persisted document so Kodi and the skin helper both
        # discard the target device's cached values before AF3 is regenerated.
        xbmc.executebuiltin('ReloadSkin()')
        self.pause(2)
        if skin != xbmc.getSkinDir():
            raise BackupError('The active skin changed while restored settings were being verified.')
        live = rpc('Settings.GetSkinSettings')
        if not isinstance(live, dict) or live.get('skin') != skin or not isinstance(live.get('settings'), list):
            raise BackupError('Kodi did not expose the reloaded skin settings.')
        current = {item.get('id'): (item.get('type'), item.get('value'))
                   for item in live.get('settings', []) if isinstance(item, dict)}
        mismatched = [item['id'] for item in checked
                      if current.get(item['id']) != (item['type'], item['value'])]
        if mismatched:
            raise BackupError('Kodi did not apply {} restored skin setting(s).'.format(len(mismatched)))

    def backup(self, manual=False, protected=False):
        with operation_lock(self.lock_path):
            if os.path.exists(self.pending_path) or self.incomplete():
                raise BackupError('Finish or recover the pending restore before making another backup.')
            skin = xbmc.getSkinDir()
            validate_skin_id(skin)
            persisted = self.persist_skin_settings(skin, force=True)
            state = self.state()
            key = self.key(skin)
            previous = state['skins'].get(key, {})
            if previous.get('blocked') and not manual:
                raise BackupError(previous['blocked'])
            # Kodi delays saves. Read twice across a quiet period before trusting this snapshot.
            first = collect_files(self.profile, skin)
            appearance = self.appearance()
            self.pause(2)
            files = collect_files(self.profile, skin)
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
            meta = {'skin_id': skin, 'skin_version': xbmcaddon.Addon(skin).getAddonInfo('version'),
                    'device_id': state['device_id'],
                    'device_name': self.addon.getSetting('device_name') or xbmc.getInfoLabel('System.FriendlyName') or 'Kodi',
                    'profile_id': hashlib.sha256(self.profile.encode()).hexdigest()[:12],
                    'created_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'protected': bool(protected), 'stats': stats, 'appearance': appearance}
            blob = build_archive(files, meta)
            manifest, _ = read_archive(blob)
            record = store.publish(blob, manifest)
            state['skins'][key] = {'last_check': time.time(), 'last_backup': meta['created_at'],
                                   'fingerprint': digest, 'stats': stats, 'archive': record['archive']}
            atomic_json(self.state_path, state)
            # Retention runs only after the new archive and completion record verified successfully.
            store.prune(max(1, self.addon.getSettingInt('keep')))
            message = 'Saved {} files ({:.1f} KB){}.'.format(
                len(files), len(blob) / 1024,
                ' as a protected snapshot' if protected else '')
            if not persisted:
                message += ' Kodi did not create settings.xml; defaults and helper data were still backed up.'
            return message

    def switch_skin(self, skin):
        if xbmc.getSkinDir() == skin:
            return
        xbmc.executebuiltin('ActivateWindow(home)')
        self.pause(0.5)
        if not rpc('Settings.SetSettingValue', setting='lookandfeel.skin', value=skin):
            raise BackupError('Kodi refused to change skins.')
        # Kodi's confirmation can revert the choice for 10 seconds. Never restore during it.
        self.pause(12)
        if xbmc.getSkinDir() != skin:
            raise BackupError('Skin change was not kept. No restore files have been written in this step.')

    def restore_blob(self, blob):
        manifest, files = read_archive(blob)
        skin = manifest['skin_id']
        try:
            installed = xbmcaddon.Addon(skin)
        except RuntimeError as exc:
            raise BackupError('Install the backed-up skin and its dependencies before restoring.') from exc
        label = '{}\n{}\nDevice: {}\n{} files'.format(skin, manifest['created_at'],
                manifest.get('device_name', 'Unknown'), len(files))
        if installed.getAddonInfo('version') != manifest.get('skin_version'):
            label += '\nSkin version differs; older settings may behave differently.'
        label += '\nReplace this skin’s saved settings? A local rollback copy will be kept.'
        if not xbmcgui.Dialog().yesno('Restore skin settings', label):
            return
        with operation_lock(self.lock_path):
            if self.incomplete() or os.path.exists(self.pending_path):
                raise BackupError('Finish or recover the previous restore first.')
            if xbmc.Player().isPlaying():
                raise BackupError('Stop playback before restoring.')
            if xbmc.getSkinDir() == skin:
                fallback = 'skin.estuary' if skin != 'skin.estuary' else 'skin.estouchy'
                try:
                    xbmcaddon.Addon(fallback)
                except RuntimeError as exc:
                    raise BackupError('Switch to a different installed skin, then run Restore again.') from exc
                xbmcgui.Dialog().ok(TITLE, 'Kodi will switch to {}. Accept “Keep this skin” so restoration can proceed.'.format(fallback))
                self.switch_skin(fallback)
            if skin == xbmc.getSkinDir():
                raise BackupError('The target skin is still active; restore stopped.')
            self.pause(2)
            if skin == xbmc.getSkinDir():
                raise BackupError('The target skin became active again; restore stopped before writing.')
            # Remember intent before mutation so a crash never looks like a completed operation.
            pending = {'skin_id': skin, 'phase': 'restoring', 'created_at': manifest['created_at'],
                       'paths': list(files), 'started_at': time.time(),
                       'known_transactions': sorted(os.listdir(self.rollback_root)) if os.path.isdir(self.rollback_root) else [],
                       'appearance': manifest.get('appearance', {}),
                       'skin_settings': skin_setting_values(files, skin)}
            atomic_json(self.pending_path, pending)
            try:
                rollback = restore_files(self.profile, skin, files, self.rollback_root)
            except Exception:
                if not self.incomplete():
                    os.unlink(self.pending_path)
                raise
            pending.update({'phase': 'rebuild', 'rollback': rollback})
            journal = read_json(os.path.join(rollback, 'transaction.json'))
            pending['paths'] = sorted(set(pending['paths']) | {e['path'] for e in journal['previous_entries']})
            atomic_json(self.pending_path, pending)
            self.clear_helper_cache(skin, files)
            # Do not treat the previous configuration's reset baseline as a new reset after restore.
            state = self.state()
            state['skins'] = {}
            atomic_json(self.state_path, state)
        if xbmcgui.Dialog().yesno(TITLE, 'Settings restored. Activate {} now? Accept Kodi’s “Keep this skin” prompt. AF3 will then rebuild its menus.'.format(skin)):
            self.switch_skin(skin)
            self.finish_restore()

    def clear_helper_cache(self, skin, files):
        win = xbmcgui.Window(10000)
        nodes = 'addon_data/script.skinvariables/nodes/'
        for path in files:
            if path.startswith(nodes):
                directory, filename = path[len(nodes):].rsplit('/', 1)
                win.clearProperty('SkinVariables.ShortcutsNode.{}-{}'.format(directory, filename))
        win.setProperty('SkinVariables.ShortcutsNode.Reload', str(time.time()))

    def finish_restore(self):
        with operation_lock(self.lock_path):
            pending = read_json(self.pending_path)
            if not pending:
                return
            if pending.get('phase') != 'rebuild' or self.incomplete():
                raise BackupError('A restore was interrupted. Choose Recover interrupted restore.')
            skin = pending['skin_id']
            if xbmc.getSkinDir() != skin:
                raise BackupError('Activate {} and accept the skin change, then choose Finish restored skin.'.format(skin))
            if 'skin_settings' in pending:
                self.apply_skin_settings(skin, pending['skin_settings'])
            self.clear_helper_cache(skin, pending.get('paths', []))
            for setting in APPEARANCE:
                if setting not in pending.get('appearance', {}):
                    continue
                value = pending['appearance'][setting]
                current = rpc('Settings.GetSettingValue', setting=setting).get('value')
                if current != value and not rpc('Settings.SetSettingValue', setting=setting, value=value):
                    raise BackupError('Kodi could not apply the restored appearance. Choose a supported theme/font and retry Finish restored skin.')
            if skin == AF3:
                self.rebuild_af3(pending)
            state = self.state()
            state['skins'] = {}
            atomic_json(self.state_path, state)
            os.unlink(self.pending_path)
            xbmcgui.Dialog().notification(TITLE, 'Restore complete.', xbmcgui.NOTIFICATION_INFO)

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
            if os.path.islink(target) or os.path.islink(os.path.dirname(target)):
                raise BackupError('Cannot safely rebuild the skin profile include.')
            root = ET.Element('includes')
            ET.SubElement(root, 'include', {'file': 'script-skinvariables-generator-includes-{}.xml'.format(slug)})
            content = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            temp = target + '.skinbackup.tmp'
            with open(temp, 'wb') as handle:
                handle.write(content)
            os.replace(temp, target)
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
        xbmcgui.Window(10000).clearProperty(prop)
        xbmc.executebuiltin('ReloadSkin()')

    def recovery(self):
        with operation_lock(self.lock_path):
            pending = read_json(self.pending_path)
            if pending and xbmc.getSkinDir() == pending.get('skin_id'):
                raise BackupError('Switch to a different skin before recovering interrupted writes.')
            if not xbmcgui.Dialog().yesno(TITLE, 'Undo any interrupted file writes using the saved local rollback copies?'):
                return
            recovered = recover_pending(self.profile, self.rollback_root)
            if pending and pending.get('phase') == 'restoring':
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
                    pending.update({'phase': 'rebuild', 'rollback': os.path.dirname(path),
                                    'paths': sorted(set(pending.get('paths', [])) | {e['path'] for e in journal['previous_entries']})})
                    atomic_json(self.pending_path, pending)
                    xbmcgui.Dialog().ok(TITLE, 'The files had already finished restoring. Activate the restored skin, then choose Finish restored skin.')
                    return
                os.unlink(self.pending_path)
            elif pending and pending.get('phase') == 'rebuild':
                xbmcgui.Dialog().ok(TITLE, 'File restoration is complete. Activate the restored skin, then choose Finish restored skin.')
                return
            xbmcgui.Dialog().ok(TITLE, 'Recovered {} interrupted transaction(s).'.format(len(recovered)))

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
            self.restore_blob(store.load(record))

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
            run(app)
            return
        options = ['Back up now', 'Save protected snapshot', 'Restore backup', 'Import backup ZIP',
                   'Choose destination', 'Settings', 'Status and coverage', 'Finish restored skin',
                   'Recover interrupted restore', 'Run self-test']
        selected = xbmcgui.Dialog().select(TITLE, options)
        if selected in (0, 1):
            xbmcgui.Dialog().ok(TITLE, app.backup(manual=True, protected=selected == 1))
        elif selected == 2:
            app.choose_restore()
        elif selected == 3:
            path = xbmcgui.Dialog().browseSingle(1, 'Choose skin backup ZIP', 'files', '.zip')
            if path:
                app.restore_blob(Store(xbmcvfs, '', os.path.join(app.data, 'staging')).read(path))
        elif selected == 4:
            path = xbmcgui.Dialog().browseSingle(0, 'Choose backup folder', 'files', treatAsFolder=True)
            if path:
                app.addon.setSetting('destination', path)
        elif selected == 5:
            app.addon.openSettings()
        elif selected == 6:
            app.status()
        elif selected == 7:
            app.finish_restore()
        elif selected == 8:
            app.recovery()
        elif selected == 9:
            from resources.lib.diagnostics import run
            result = run(app)
            xbmcgui.Dialog().textviewer(TITLE, json.dumps(result, indent=2))
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
                    pending = read_json(app.pending_path)
                    if pending.get('phase') == 'rebuild' and xbmc.getSkinDir() == pending.get('skin_id'):
                        app.finish_restore()
                        last_error = ''
                    elif pending.get('phase') == 'restoring':
                        raise BackupError('A restore was interrupted. Open Skin Settings Backup and choose Recover interrupted restore.')
                else:
                    if now >= next_guard and not app.skin_settings_valid(xbmc.getSkinDir()):
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
