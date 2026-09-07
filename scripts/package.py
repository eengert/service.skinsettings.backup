#!/usr/bin/env python3
"""Build a deterministic installable ZIP without tests, Git or macOS metadata."""
import hashlib
import pathlib
import zipfile
import xml.etree.ElementTree as ET


def main():
    root = pathlib.Path(__file__).resolve().parents[1]
    metadata = ET.parse(root / 'addon.xml').getroot()
    addon_id, version = metadata.attrib['id'], metadata.attrib['version']
    output = root / 'dist' / '{}-{}.zip'.format(addon_id, version)
    output.parent.mkdir(exist_ok=True)
    files = [root / name for name in ('addon.xml', 'default.py', 'service.py', 'LICENSE.txt', 'README.md')]
    for asset in metadata.findall('./extension[@point="xbmc.addon.metadata"]/assets/*'):
        if asset.text:
            path = root / asset.text
            if not path.is_file() or not path.resolve().is_relative_to(root):
                raise SystemExit('Missing or unsafe add-on asset: ' + asset.text)
            files.append(path)
    files += [path for path in (root / 'resources').rglob('*') if path.is_file()
              and '__pycache__' not in path.parts and not path.name.startswith(('.', '._'))
              and path.suffix not in ('.pyc', '.pyo')]
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(addon_id + '/' + path.relative_to(root).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    with zipfile.ZipFile(output) as archive:
        damaged = archive.testzip()
        if damaged is not None:
            raise SystemExit('Package verification failed for: ' + damaged)
        if not all(name.startswith(addon_id + '/') for name in archive.namelist()):
            raise SystemExit('Package contains a file outside the add-on directory')
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text('{}  {}\n'.format(digest, output.name))
    print('{} ({} bytes)\nSHA256 {}'.format(output, output.stat().st_size, digest))


if __name__ == '__main__':
    main()
