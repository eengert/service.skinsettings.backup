"""Small, deterministic scheduling and reset-detection rules."""
import hashlib
import xml.etree.ElementTree as ET


def fingerprint(files):
    digest = hashlib.sha256()
    for path, data in sorted(files.items()):
        digest.update(path.encode('utf-8') + b'\0')
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def statistics(files, skin_id):
    root = ET.fromstring(files['addon_data/{}/settings.xml'.format(skin_id)])
    settings = list(root.findall('setting'))
    meaningful = sum(1 for item in settings if
                     (item.text or item.get('value') or '').strip().lower()
                     not in ('', 'false', '0', 'none'))
    return {'settings': len(settings), 'meaningful': meaningful,
            'helpers': sum(path.endswith('.json') for path in files),
            'bytes': sum(len(data) for data in files.values())}


def reset_reason(previous, current):
    if not previous:
        return ''
    for key, label, floor in (
        ('settings', 'skin settings', 10),
        ('meaningful', 'configured values', 6),
        ('helpers', 'menu/widget files', 2),
    ):
        before = previous.get(key, 0)
        after = current.get(key, 0)
        if before >= floor and after < before * 0.5:
            return 'The number of {} fell from {} to {}.'.format(label, before, after)
    return ''


def due(last_check, now, interval_hours):
    # A clock correction must not disable backups indefinitely.
    return not last_check or now < last_check or now - last_check >= interval_hours * 3600
