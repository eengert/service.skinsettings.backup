"""Safe, Kodi-independent archive handling for skin settings backups."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Mapping, Tuple
from xml.etree import ElementTree


SCHEMA_VERSION = 1
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_TOTAL_SIZE = 50 * 1024 * 1024
MAX_FILES = 2000
MAX_MANIFEST_SIZE = 2 * 1024 * 1024
MAX_ARCHIVE_SIZE = 55 * 1024 * 1024
_SKIN_ID = re.compile(r"^skin\.[A-Za-z0-9][A-Za-z0-9._-]{0,122}$")
_METADATA_FIELDS = (
    "skin_id",
    "skin_version",
    "device_id",
    "device_name",
    "profile_id",
    "created_at",
    "protected",
)
_APPEARANCE_STRING_FIELDS = {
    "lookandfeel.skintheme",
    "lookandfeel.skincolors",
    "lookandfeel.font",
}
_APPEARANCE_INTEGER_FIELDS = {"lookandfeel.skinzoom"}
JOURNAL_NAME = "transaction.json"
_ROLLBACK_FILES = "files"
_SKIN_USER_SLUG = re.compile(r"^user-[0-9A-Za-z]+$")
_EMPTY_SKIN_SETTINGS = b'<?xml version="1.0" encoding="UTF-8"?><settings />'


class BackupError(Exception):
    """Raised when a backup cannot be safely created, read, or restored."""


def validate_skin_id(skin_id: str) -> str:
    """Validate and return a Kodi skin add-on identifier."""
    if not isinstance(skin_id, str) or not _SKIN_ID.fullmatch(skin_id):
        raise BackupError("invalid skin id")
    if ".." in skin_id:
        raise BackupError("invalid skin id")
    return skin_id


def _settings_path(skin_id: str) -> str:
    return f"addon_data/{skin_id}/settings.xml"


def _helper_roots(skin_id: str, skin_user_slugs: Iterable[str] = ()) -> Tuple[str, ...]:
    base = "addon_data/script.skinvariables"
    node_ids = [skin_id] + [f"{skin_id}-{slug}" for slug in sorted(set(skin_user_slugs))]
    return tuple(f"{base}/nodes/{node_id}" for node_id in node_ids) + (f"{base}/logins/{skin_id}",)


def _viewtypes_path(skin_id: str) -> str:
    return f"addon_data/script.skinvariables/{skin_id}-viewtypes.json"


def _validate_path_syntax(path: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path or ":" in path:
        raise BackupError("invalid archive path")
    pure = PurePosixPath(path)
    if (len(path) > 1024 or pure.is_absolute() or str(pure) != path or
            any(p in ("", ".", "..") or len(p) > 255 for p in pure.parts)):
        raise BackupError("invalid archive path")
    return path


def _validate_relative_path(path: str, skin_id: str, skin_user_slugs: Iterable[str] = ()) -> str:
    _validate_path_syntax(path)
    if path == _settings_path(skin_id) or path == _viewtypes_path(skin_id):
        return path
    for root in _helper_roots(skin_id, skin_user_slugs):
        prefix = root + "/"
        if path.startswith(prefix) and len(path) > len(prefix) and path.endswith(".json"):
            return path
    raise BackupError(f"path is outside the managed backup scope: {path}")


def _path_signature(info: os.stat_result) -> Tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _assert_root_directory(root: Path) -> None:
    try:
        info = root.lstat()
    except OSError as exc:
        raise BackupError(f"profile directory is unavailable: {root}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BackupError(f"profile path is not a safe directory: {root}")


def _assert_no_symlink_components(root: Path, relative: str, include_leaf: bool = True) -> None:
    current = root
    parts = PurePosixPath(relative).parts
    if not include_leaf:
        parts = parts[:-1]
    for part in parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BackupError(f"cannot inspect path: {relative}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise BackupError(f"symlinks are not allowed in managed paths: {relative}")


def _skinusers_path(skin_id: str) -> str:
    return f"addon_data/script.skinvariables/logins/{skin_id}/skinusers.json"


def _skin_user_slugs(data: bytes | None) -> Tuple[str, ...]:
    if data is None:
        return ()
    users = _load_json(data, "Skin Variables skinusers.json")
    if not isinstance(users, list):
        raise BackupError("Skin Variables skinusers.json must contain a list")
    slugs = []
    for user in users:
        if not isinstance(user, dict):
            raise BackupError("invalid Skin Variables skin user")
        slug = user.get("slug")
        if not isinstance(slug, str) or not _SKIN_USER_SLUG.fullmatch(slug):
            raise BackupError("invalid Skin Variables skin user slug")
        if slug in slugs:
            raise BackupError("duplicate Skin Variables skin user slug")
        slugs.append(slug)
    return tuple(sorted(slugs))


def _current_skin_user_slugs(root: Path, skin_id: str) -> Tuple[str, ...]:
    relative = _skinusers_path(skin_id)
    _assert_no_symlink_components(root, relative)
    path = root.joinpath(*PurePosixPath(relative).parts)
    if not path.exists():
        return ()
    return _skin_user_slugs(_read_consistent(path))


def _inferred_skin_user_slugs(root: Path, skin_id: str) -> Tuple[str, ...]:
    """Find only helper-standard sibling node directories owned by this skin."""
    relative = "addon_data/script.skinvariables/nodes"
    _assert_no_symlink_components(root, relative)
    nodes = root.joinpath(*PurePosixPath(relative).parts)
    if not nodes.exists():
        return ()
    try:
        info = nodes.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise BackupError("Skin Variables nodes path is not a safe directory")
        entries = tuple(os.scandir(nodes))
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError("cannot inspect Skin Variables profile directories") from exc
    prefix = skin_id + "-"
    found = []
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        slug = entry.name[len(prefix):]
        if not _SKIN_USER_SLUG.fullmatch(slug):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise BackupError(f"unsafe Skin Variables profile directory: {entry.name}")
        except OSError as exc:
            raise BackupError(f"cannot inspect Skin Variables profile directory: {entry.name}") from exc
        found.append(slug)
    return tuple(sorted(found))


def _file_skin_user_slugs(paths: Iterable[str], skin_id: str) -> Tuple[str, ...]:
    """Infer helper-standard profile slugs from an already bounded path set."""
    prefix = f"addon_data/script.skinvariables/nodes/{skin_id}-"
    found = []
    for path in paths:
        if not isinstance(path, str) or not path.startswith(prefix):
            continue
        directory = path[len(prefix):].split("/", 1)[0]
        if _SKIN_USER_SLUG.fullmatch(directory):
            found.append(directory)
    return tuple(sorted(set(found)))


def _enumerate_managed(root: Path, skin_id: str, skin_user_slugs: Iterable[str] = ()) -> Tuple[str, ...]:
    _assert_root_directory(root)
    found = []
    for fixed in (_settings_path(skin_id), _viewtypes_path(skin_id)):
        _assert_no_symlink_components(root, fixed)
        path = root.joinpath(*PurePosixPath(fixed).parts)
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise BackupError(f"managed path is not a regular file: {fixed}")
            found.append(fixed)

    for relative_root in _helper_roots(skin_id, skin_user_slugs):
        _assert_no_symlink_components(root, relative_root)
        directory = root.joinpath(*PurePosixPath(relative_root).parts)
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise BackupError(f"managed helper root is not a directory: {relative_root}")
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            base = Path(dirpath)
            for name in tuple(dirnames) + tuple(filenames):
                child = base / name
                try:
                    info = child.lstat()
                except OSError as exc:
                    raise BackupError(f"cannot inspect managed path: {child}") from exc
                if stat.S_ISLNK(info.st_mode):
                    raise BackupError(f"symlinks are not allowed in managed paths: {child}")
            for name in filenames:
                if not name.endswith(".json"):
                    continue
                child = base / name
                info = child.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise BackupError(f"managed path is not a regular file: {child}")
                relative = child.relative_to(root).as_posix()
                _validate_relative_path(relative, skin_id, skin_user_slugs)
                found.append(relative)
    if len(found) > MAX_FILES:
        raise BackupError("backup contains too many files")
    return tuple(sorted(found))


def _read_once(path: Path) -> Tuple[bytes, Tuple[int, int, int, int, int]]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise BackupError(f"managed path is not a regular file: {path}")
        if before.st_size > MAX_FILE_SIZE:
            raise BackupError(f"managed file exceeds the size limit: {path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_FILE_SIZE + 1)
            after_fd = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = path.lstat()
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"cannot read managed file: {path}") from exc
    signatures = {_path_signature(before), _path_signature(opened), _path_signature(after_fd), _path_signature(after)}
    if len(signatures) != 1:
        raise BackupError(f"managed file changed while being read: {path}")
    if len(data) > MAX_FILE_SIZE:
        raise BackupError(f"managed file exceeds the size limit: {path}")
    return data, _path_signature(after)


def _read_consistent(path: Path) -> bytes:
    first, first_signature = _read_once(path)
    second, second_signature = _read_once(path)
    if first_signature != second_signature or first != second:
        raise BackupError(f"managed file changed while being read: {path}")
    return first


def _read_snapshot(root: Path, paths: Iterable[str]) -> Dict[str, bytes]:
    files = {}
    total = 0
    for relative in paths:
        data = _read_consistent(root.joinpath(*PurePosixPath(relative).parts))
        total += len(data)
        if total > MAX_TOTAL_SIZE:
            raise BackupError("managed files exceed the total size limit")
        files[relative] = data
    return files


def _validate_settings(data: bytes) -> None:
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, ValueError) as exc:
        raise BackupError("settings.xml is not valid XML") from exc
    if root.tag != "settings" or any(child.tag != "setting" for child in root):
        raise BackupError("settings.xml must contain a Kodi <settings> document")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _load_json(data: bytes, description: str) -> object:
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BackupError(f"invalid JSON in {description}") from exc


def _validate_json(data: bytes, path: str) -> None:
    _load_json(data, path)


def _validate_files(files: Mapping[str, bytes], skin_id: str, require_settings: bool = True) -> Dict[str, bytes]:
    if not isinstance(files, Mapping):
        raise BackupError("files must be a path-to-bytes mapping")
    if len(files) > MAX_FILES:
        raise BackupError("backup contains too many files")
    checked: Dict[str, bytes] = {}
    skinusers_data = files.get(_skinusers_path(skin_id))
    declared_slugs = _skin_user_slugs(skinusers_data) if skinusers_data is not None else ()
    skin_user_slugs = tuple(sorted(set(declared_slugs) | set(_file_skin_user_slugs(files, skin_id))))
    total = 0
    for path, data in files.items():
        valid_path = _validate_relative_path(path, skin_id, skin_user_slugs)
        if valid_path in checked:
            raise BackupError(f"duplicate managed path: {valid_path}")
        if not isinstance(data, bytes):
            raise BackupError(f"file content must be bytes: {valid_path}")
        if len(data) > MAX_FILE_SIZE:
            raise BackupError(f"file exceeds the size limit: {valid_path}")
        total += len(data)
        if total > MAX_TOTAL_SIZE:
            raise BackupError("backup exceeds the total size limit")
        if valid_path == _settings_path(skin_id):
            _validate_settings(data)
        else:
            _validate_json(data, valid_path)
        checked[valid_path] = data
    if require_settings and _settings_path(skin_id) not in checked:
        raise BackupError("backup is missing settings.xml")
    return checked


def collect_files(profile_path: os.PathLike | str, skin_id: str) -> Dict[str, bytes]:
    """Collect and validate a consistent snapshot of the managed files."""
    validate_skin_id(skin_id)
    root = Path(profile_path)
    skin_user_slugs = tuple(sorted(set(_current_skin_user_slugs(root, skin_id)) |
                                   set(_inferred_skin_user_slugs(root, skin_id))))
    first_paths = _enumerate_managed(root, skin_id, skin_user_slugs)
    settings = _settings_path(skin_id)
    files = _read_snapshot(root, first_paths)
    current_slugs = tuple(sorted(set(_current_skin_user_slugs(root, skin_id)) |
                                 set(_inferred_skin_user_slugs(root, skin_id))))
    if skin_user_slugs != current_slugs or first_paths != _enumerate_managed(root, skin_id, skin_user_slugs):
        raise BackupError("managed files changed while being collected")
    # Kodi treats a missing skin settings file as an empty/default settings set.
    # Preserve that state explicitly so helper data can still be backed up and a
    # restore can reliably return the skin to the same defaults.
    if settings not in files:
        files[settings] = _EMPTY_SKIN_SETTINGS
    return _validate_files(files, skin_id)


def _validate_metadata(metadata: Mapping[str, object]) -> Dict[str, object]:
    if not isinstance(metadata, Mapping):
        raise BackupError("metadata must be a mapping")
    missing = [field for field in _METADATA_FIELDS if field not in metadata]
    if missing:
        raise BackupError("metadata is missing: " + ", ".join(missing))
    result = {field: metadata[field] for field in _METADATA_FIELDS}
    validate_skin_id(result["skin_id"])
    for field in _METADATA_FIELDS[1:6]:
        if not isinstance(result[field], str) or not result[field] or len(result[field]) > 256:
            raise BackupError(f"metadata field must be a non-empty string: {field}")
    if not isinstance(result["protected"], bool):
        raise BackupError("metadata protected field must be boolean")
    try:
        value = str(result["created_at"])
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise BackupError("created_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise BackupError("created_at must be UTC")
    appearance = metadata.get("appearance", {})
    if not isinstance(appearance, Mapping):
        raise BackupError("metadata appearance field must be a mapping")
    unknown = set(appearance) - _APPEARANCE_STRING_FIELDS - _APPEARANCE_INTEGER_FIELDS
    if unknown:
        raise BackupError("metadata appearance contains unknown settings")
    checked_appearance = {}
    for key, value in appearance.items():
        if key in _APPEARANCE_STRING_FIELDS:
            if not isinstance(value, str) or not value or len(value) > 256:
                raise BackupError(f"appearance setting must be a non-empty string of at most 256 characters: {key}")
        elif not isinstance(value, int) or isinstance(value, bool) or not -100 <= value <= 100:
            raise BackupError(f"appearance setting must be an integer from -100 to 100: {key}")
        checked_appearance[key] = value
    result["appearance"] = checked_appearance
    return result


def _manifest(metadata: Mapping[str, object], files: Mapping[str, bytes]) -> Dict[str, object]:
    result: Dict[str, object] = {"schema_version": SCHEMA_VERSION}
    result.update(_validate_metadata(metadata))
    result["entries"] = [
        {"path": path, "size": len(files[path]), "sha256": hashlib.sha256(files[path]).hexdigest()}
        for path in sorted(files)
    ]
    return result


def build_archive(files: Mapping[str, bytes], metadata: Mapping[str, object]) -> bytes:
    """Build a ZIP archive with a checksummed JSON manifest."""
    checked_metadata = _validate_metadata(metadata)
    checked_files = _validate_files(files, str(checked_metadata["skin_id"]))
    manifest = _manifest(checked_metadata, checked_files)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        for path in sorted(checked_files):
            archive.writestr("files/" + path, checked_files[path])
    blob = output.getvalue()
    if len(blob) > MAX_ARCHIVE_SIZE:
        raise BackupError("archive exceeds the size limit")
    return blob


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def read_archive(blob: bytes) -> Tuple[Dict[str, object], Dict[str, bytes]]:
    """Read an archive, enforcing its scope, limits, schema, and checksums."""
    if not isinstance(blob, bytes):
        raise BackupError("archive must be bytes")
    if len(blob) > MAX_ARCHIVE_SIZE:
        raise BackupError("archive exceeds the size limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError("invalid backup archive") from exc
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise BackupError("archive contains duplicate members")
        if "manifest.json" not in names:
            raise BackupError("archive is missing manifest.json")
        if len(infos) > MAX_FILES + 1:
            raise BackupError("archive contains too many files")
        for info in infos:
            name = info.filename
            if "\\" in name or name.startswith("/") or "\x00" in name or any(p in ("", ".", "..") for p in PurePosixPath(name).parts):
                raise BackupError("archive contains an unsafe member path")
            if info.is_dir() or _zip_member_is_symlink(info) or info.flag_bits & 0x1:
                raise BackupError("archive contains an unsupported member")
            limit = MAX_MANIFEST_SIZE if name == "manifest.json" else MAX_FILE_SIZE
            if info.file_size > limit:
                raise BackupError("archive member exceeds the size limit")
        manifest_info = archive.getinfo("manifest.json")
        try:
            manifest_data = archive.read(manifest_info)
            manifest = _load_json(manifest_data, "archive manifest")
        except (KeyError, RuntimeError, NotImplementedError, zipfile.BadZipFile, BackupError) as exc:
            raise BackupError("invalid archive manifest") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
            raise BackupError("unsupported archive schema")
        metadata = _validate_metadata(manifest)
        # Normalize legacy schema-1 manifests created before appearance was recorded.
        manifest["appearance"] = metadata["appearance"]
        entries = manifest.get("entries")
        if not isinstance(entries, list) or len(entries) > MAX_FILES:
            raise BackupError("invalid archive entries")
        expected = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
                raise BackupError("invalid archive entry")
            path = _validate_path_syntax(entry.get("path"))
            if path in expected:
                raise BackupError("manifest contains duplicate paths")
            size, digest = entry.get("size"), entry.get("sha256")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > MAX_FILE_SIZE:
                raise BackupError("invalid archive entry size")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise BackupError("invalid archive entry checksum")
            expected[path] = (size, digest)
        if sum(size for size, _digest in expected.values()) > MAX_TOTAL_SIZE:
            raise BackupError("archive exceeds the total uncompressed size limit")
        member_paths = {name[6:] for name in names if name.startswith("files/")}
        if set(expected) != member_paths or any(name != "manifest.json" and not name.startswith("files/") for name in names):
            raise BackupError("archive members do not match the manifest")
        skinusers_relative = _skinusers_path(str(metadata["skin_id"]))
        skinusers_data = None
        if skinusers_relative in expected:
            try:
                skinusers_data = archive.read("files/" + skinusers_relative)
            except (RuntimeError, NotImplementedError, zipfile.BadZipFile, OSError) as exc:
                raise BackupError("cannot read Skin Variables user declarations") from exc
            expected_size, expected_digest = expected[skinusers_relative]
            if len(skinusers_data) != expected_size or hashlib.sha256(skinusers_data).hexdigest() != expected_digest:
                raise BackupError("Skin Variables user declarations failed verification")
        skin_user_slugs = tuple(sorted(set(_skin_user_slugs(skinusers_data)) |
                                       set(_file_skin_user_slugs(expected, str(metadata["skin_id"])))))
        for path in expected:
            _validate_relative_path(path, str(metadata["skin_id"]), skin_user_slugs)
        files = {}
        total = 0
        for path, (size, digest) in expected.items():
            try:
                data = archive.read("files/" + path)
            except (RuntimeError, NotImplementedError, zipfile.BadZipFile, OSError) as exc:
                raise BackupError(f"cannot read archive member: {path}") from exc
            total += len(data)
            if total > MAX_TOTAL_SIZE or len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise BackupError(f"archive content verification failed: {path}")
            files[path] = data
    checked = _validate_files(files, str(metadata["skin_id"]))
    return manifest, checked


def _fsync_directory(directory: Path) -> None:
    # Windows cannot open a directory for fsync; atomic replacement still protects file contents.
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BackupError(f"cannot sync directory: {directory}") from exc


def _ensure_safe_directory(root: Path, relative_parent: str) -> Path:
    current = root
    for part in PurePosixPath(relative_parent).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir()
                _fsync_directory(current.parent)
            except FileExistsError:
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise BackupError(f"unsafe parent directory: {current}")
            except OSError as exc:
                raise BackupError(f"cannot create directory: {current}") from exc
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise BackupError(f"unsafe parent directory: {current}")
    return current


def _atomic_write_impl(root: Path, relative: str, data: bytes) -> None:
    parent_relative = str(PurePosixPath(relative).parent)
    parent = root if parent_relative == "." else _ensure_safe_directory(root, parent_relative)
    destination = root.joinpath(*PurePosixPath(relative).parts)
    _assert_no_symlink_components(root, relative)
    descriptor = -1
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".backup-", dir=parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(parent)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"cannot atomically write: {relative}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _atomic_write(root: Path, relative: str, data: bytes) -> None:
    """Patch point for testing failures during destination writes."""
    _atomic_write_impl(root, relative, data)


def _journal_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_journal(rollback_directory: Path, journal: Dict[str, object], status: str) -> None:
    journal["status"] = status
    _atomic_write_impl(rollback_directory, JOURNAL_NAME, _journal_bytes(journal))


def _remove_managed(root: Path, paths: Iterable[str], skin_id: str, skin_user_slugs: Iterable[str] = ()) -> None:
    for relative in sorted(set(paths), reverse=True):
        _validate_relative_path(relative, skin_id, skin_user_slugs)
        _assert_no_symlink_components(root, relative)
        path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise BackupError(f"refusing to remove unsafe managed path: {relative}")
        try:
            path.unlink()
            _fsync_directory(path.parent)
        except OSError as exc:
            raise BackupError(f"cannot remove managed file: {relative}") from exc


def _restore_snapshot(root: Path, skin_id: str, rollback_directory: Path, journal: Mapping[str, object]) -> None:
    previous_entries = journal.get("previous_entries")
    target_paths = journal.get("target_paths")
    if (not isinstance(previous_entries, list) or not isinstance(target_paths, list) or
            len(previous_entries) > MAX_FILES or len(target_paths) > MAX_FILES):
        raise BackupError("invalid rollback journal")
    skin_user_slugs = journal.get("skin_user_slugs")
    if not isinstance(skin_user_slugs, list) or any(not isinstance(slug, str) or not _SKIN_USER_SLUG.fullmatch(slug) for slug in skin_user_slugs):
        raise BackupError("invalid rollback skin user declarations")
    previous = {}
    total = 0
    for entry in previous_entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise BackupError("invalid rollback journal entry")
        relative = _validate_relative_path(entry.get("path"), skin_id, skin_user_slugs)
        snapshot_relative = f"{_ROLLBACK_FILES}/{relative}"
        _assert_no_symlink_components(rollback_directory, snapshot_relative)
        snapshot = rollback_directory.joinpath(*PurePosixPath(snapshot_relative).parts)
        data = _read_consistent(snapshot)
        if len(data) != entry.get("size") or hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise BackupError("rollback snapshot verification failed")
        if relative in previous:
            raise BackupError("duplicate rollback journal entry")
        total += len(data)
        if total > MAX_TOTAL_SIZE:
            raise BackupError("rollback snapshot exceeds the total size limit")
        previous[relative] = data
    for relative in target_paths:
        _validate_relative_path(relative, skin_id, skin_user_slugs)
    current = _enumerate_managed(root, skin_id, skin_user_slugs)
    _remove_managed(root, set(current) | set(target_paths), skin_id, skin_user_slugs)
    for relative, data in sorted(previous.items()):
        _atomic_write_impl(root, relative, data)


def restore_files(
    profile_path: os.PathLike | str,
    skin_id: str,
    files: Mapping[str, bytes],
    rollback_root: os.PathLike | str,
) -> str:
    """Transactionally replace the managed files and retain a rollback snapshot."""
    validate_skin_id(skin_id)
    checked = _validate_files(files, skin_id)
    root = Path(profile_path)
    _assert_root_directory(root)
    try:
        declared_current_slugs = _current_skin_user_slugs(root, skin_id)
    except BackupError:
        # Restore is also a repair path. The corrupt declaration itself is snapshotted raw below.
        declared_current_slugs = ()
    current_slugs = tuple(sorted(set(declared_current_slugs) | set(_inferred_skin_user_slugs(root, skin_id))))
    incoming_slugs = tuple(sorted(set(_skin_user_slugs(checked.get(_skinusers_path(skin_id)))) |
                                  set(_file_skin_user_slugs(checked, skin_id))))
    transaction_slugs = tuple(sorted(set(current_slugs) | set(incoming_slugs)))
    existing_paths = _enumerate_managed(root, skin_id, transaction_slugs)
    existing = _read_snapshot(root, existing_paths)

    rollback_base = Path(rollback_root)
    try:
        rollback_base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError("cannot create rollback root") from exc
    _assert_root_directory(rollback_base)
    try:
        rollback_directory = Path(tempfile.mkdtemp(prefix=f"{skin_id}-", dir=rollback_base))
    except OSError as exc:
        raise BackupError("cannot create rollback directory") from exc
    journal: Dict[str, object] = {
        "version": 1,
        "transaction_id": str(uuid.uuid4()),
        "profile_path": os.path.abspath(os.fspath(root)),
        "skin_id": skin_id,
        "skin_user_slugs": list(transaction_slugs),
        "previous_entries": [],
        "target_paths": sorted(checked),
    }
    # Record intent before copying the rollback snapshot. A crash here cannot have
    # changed target files, so recovery can safely close a snapshotting transaction.
    _write_journal(rollback_directory, journal, "snapshotting")
    previous_entries = []
    try:
        for relative, data in sorted(existing.items()):
            snapshot_relative = f"{_ROLLBACK_FILES}/{relative}"
            _atomic_write_impl(rollback_directory, snapshot_relative, data)
            previous_entries.append({"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    except Exception as exc:
        try:
            _write_journal(rollback_directory, journal, "rolled_back")
        except Exception:
            pass
        raise BackupError("could not save the rollback snapshot; target files were unchanged") from exc
    journal["previous_entries"] = previous_entries
    _write_journal(rollback_directory, journal, "prepared")
    try:
        _write_journal(rollback_directory, journal, "applying")
        _remove_managed(root, set(existing) - set(checked), skin_id, transaction_slugs)
        for relative, data in sorted(checked.items()):
            _atomic_write(root, relative, data)
        _write_journal(rollback_directory, journal, "complete")
    except Exception as exc:
        try:
            _restore_snapshot(root, skin_id, rollback_directory, journal)
            _write_journal(rollback_directory, journal, "rolled_back")
        except Exception as rollback_exc:
            try:
                _write_journal(rollback_directory, journal, "rollback_failed")
            except Exception:
                pass
            raise BackupError(f"restore failed and rollback failed: {rollback_exc}") from exc
        if isinstance(exc, BackupError):
            raise
        raise BackupError("restore failed; previous files were restored") from exc
    return str(rollback_directory)


def rollback_restore(
    profile_path: os.PathLike | str,
    rollback_directory: os.PathLike | str,
    expected_skin_id: str | None = None,
) -> str:
    """Roll back one completed restore transaction from its exact snapshot directory."""
    root = Path(profile_path)
    _assert_root_directory(root)
    directory = Path(rollback_directory)
    _assert_root_directory(directory)
    journal_path = directory / JOURNAL_NAME
    if journal_path.is_symlink() or not journal_path.exists():
        raise BackupError(f"rollback journal is unavailable: {directory}")
    try:
        raw = _read_consistent(journal_path)
        if len(raw) > MAX_MANIFEST_SIZE:
            raise BackupError("rollback journal is too large")
        journal = _load_json(raw, "rollback journal")
    except BackupError as exc:
        raise BackupError(f"invalid rollback journal: {directory}") from exc
    if not isinstance(journal, dict) or journal.get("version") != 1:
        raise BackupError(f"invalid rollback journal: {directory}")
    if journal.get("profile_path") != os.path.abspath(os.fspath(root)):
        raise BackupError("rollback journal does not belong to this profile")
    status = journal.get("status")
    if status not in ("complete", "rolled_back"):
        raise BackupError(f"rollback transaction is not complete: {directory}")
    skin_id = journal.get("skin_id")
    validate_skin_id(skin_id)
    if expected_skin_id is not None and skin_id != validate_skin_id(expected_skin_id):
        raise BackupError("rollback journal belongs to a different skin")
    try:
        _restore_snapshot(root, skin_id, directory, journal)
        _write_journal(directory, journal, "rolled_back")
    except Exception as exc:
        try:
            _write_journal(directory, journal, "rollback_failed")
        except Exception:
            pass
        raise BackupError(f"rollback failed: {directory}") from exc
    return str(directory)


def recover_pending(profile_path: os.PathLike | str, rollback_root: os.PathLike | str) -> list[str]:
    """Recover applying or failed transactions for this exact profile path."""
    root = Path(profile_path)
    _assert_root_directory(root)
    rollback_base = Path(rollback_root)
    if not rollback_base.exists():
        return []
    _assert_root_directory(rollback_base)
    recovered = []
    for directory in sorted(path for path in rollback_base.iterdir() if path.is_dir() and not path.is_symlink()):
        journal_path = directory / JOURNAL_NAME
        if not journal_path.exists() or journal_path.is_symlink():
            continue
        try:
            raw = _read_consistent(journal_path)
            if len(raw) > MAX_MANIFEST_SIZE:
                raise BackupError("rollback journal is too large")
            journal = _load_json(raw, "rollback journal")
        except BackupError as exc:
            raise BackupError(f"invalid rollback journal: {directory}") from exc
        if not isinstance(journal, dict):
            raise BackupError(f"invalid rollback journal: {directory}")
        if journal.get("profile_path") != os.path.abspath(os.fspath(root)):
            continue
        if journal.get("version") != 1:
            raise BackupError(f"invalid rollback journal: {directory}")
        status = journal.get("status")
        if status in ("complete", "rolled_back"):
            continue
        if status not in ("snapshotting", "prepared", "applying", "rollback_failed"):
            raise BackupError(f"invalid rollback status: {directory}")
        skin_id = journal.get("skin_id")
        validate_skin_id(skin_id)
        if status == "snapshotting":
            _write_journal(directory, journal, "rolled_back")
            recovered.append(str(directory))
            continue
        try:
            _restore_snapshot(root, skin_id, directory, journal)
            _write_journal(directory, journal, "rolled_back")
        except Exception as exc:
            try:
                _write_journal(directory, journal, "rollback_failed")
            except Exception:
                pass
            raise BackupError(f"pending rollback failed: {directory}") from exc
        recovered.append(str(directory))
    return recovered
