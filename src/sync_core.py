"""Safety-first reconcile core for AnchorSync.

The normal sync path is deliberately boring:

* a Calibre UUID is the identity of a book;
* the manifest locks the filename for every existing book;
* a normal sync may copy or update file *content* under that locked name;
* a normal sync never renames, deletes, or writes a KOReader sidecar.

Removing a book from the folder is a separate, previewed, trash-only Cleanup
action.  A write-ahead journal makes interrupted syncs and cleanups recoverable.
"""

import hashlib
import json
import os
import re
import shutil
import socket
import time
import unicodedata
import uuid


MANIFEST_VERSION = 2
NEW_BOOK_TEMPLATE = '{title} - {authors}'
SUPPORTED_EXTENSIONS = {'.epub', '.pdf'}
STAGE_PREFIX = re.compile(r'^__(\d+)__(.+)$')
CONFLICT_PATTERN = re.compile(r'\.sync-conflict[-.]', re.IGNORECASE)
TEMP_PREFIX = '.anchorsync-'
CLEANUP_CONFIRM_THRESHOLD = 10
MANIFEST_BACKUPS = 3
MAX_FILENAME_BYTES = 200
MAX_FULL_PATH_LENGTH = 250


class SyncError(Exception):
    pass


class ManifestError(SyncError):
    pass


class LockError(SyncError):
    pass


class CleanupError(SyncError):
    pass


class RestoreError(SyncError):
    pass


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _utc_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _hostname():
    try:
        return socket.gethostname()
    except OSError:
        return ''


def _name_key(name):
    return unicodedata.normalize('NFC', name).casefold()


def _extension(name):
    return os.path.splitext(name)[1].lower()


def _truncate_utf8(text, limit):
    result = []
    used = 0
    for char in text:
        size = len(char.encode('utf-8'))
        if used + size > limit:
            break
        result.append(char)
        used += size
    return ''.join(result)


def _short_uuid(key):
    compact = re.sub(r'[^0-9a-fA-F]', '', str(key))
    if len(compact) >= 8:
        return compact[:8].lower()
    return hashlib.sha1(str(key).encode('utf-8')).hexdigest()[:8]


def _safe_basename(name):
    if not isinstance(name, str) or not name or name in ('.', '..'):
        return False
    if '\x00' in name:
        return False
    if '/' in name or '\\' in name or os.path.basename(name) != name:
        return False
    return True


def _is_book_filename(name):
    return _extension(name) in SUPPORTED_EXTENSIONS


def is_conflict_filename(name):
    return bool(CONFLICT_PATTERN.search(name))


def sidecar_name(filename):
    return os.path.splitext(filename)[0] + '.sdr'


def sidecar_path(book_dir, filename):
    return os.path.join(book_dir, sidecar_name(filename))


def _canonical_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _paths_overlap(first, second):
    if not first or not second:
        return False
    first = _canonical_path(first)
    second = _canonical_path(second)
    try:
        common = os.path.commonpath([first, second])
    except ValueError:
        return False
    return common in (first, second)


def validate_book_dir(book_dir, library_dir='', trash_dir=''):
    if not book_dir or not str(book_dir).strip():
        raise SyncError('The KOReader book folder is not configured.')
    if os.path.exists(book_dir) and not os.path.isdir(book_dir):
        raise SyncError('The KOReader book folder is not a folder: %s' % book_dir)
    if library_dir and _paths_overlap(book_dir, library_dir):
        raise SyncError('The KOReader book folder must be separate from the Calibre library folder.')
    if trash_dir and _paths_overlap(book_dir, trash_dir):
        raise SyncError('The deleted-book trash folder must be separate from the KOReader book folder.')
    if trash_dir and library_dir and _paths_overlap(trash_dir, library_dir):
        raise SyncError('The deleted-book trash folder must be separate from the Calibre library folder.')


def _validate_stage_name(name):
    if not _safe_basename(name):
        raise SyncError('Calibre produced an unsafe filename: %r' % (name,))
    stem = os.path.splitext(name)[0]
    if not stem or stem.startswith(TEMP_PREFIX):
        raise SyncError('Calibre produced an unsafe filename: %r' % (name,))
    if name.endswith(' ') or name.endswith('.'):
        raise SyncError('Calibre produced a filename that Windows cannot store safely: %r' % (name,))
    if any(ord(char) < 32 for char in name):
        raise SyncError('Calibre produced a filename containing control characters: %r' % (name,))
    if any(char in name for char in '<>:"|?*'):
        raise SyncError(
            'Calibre produced a filename that is not portable across platforms: %r' % (name,)
        )
    if is_conflict_filename(name):
        raise SyncError(
            'Calibre produced a filename that looks like a Syncthing conflict copy: %r. '
            'Change the filename template or the book metadata.' % (name,)
        )
    reserved = {
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
    }
    if stem.upper() in reserved:
        raise SyncError('Calibre produced a reserved Windows filename: %r' % (name,))


def validate_template(template):
    """Validate the template used to name new books.

    Existing names are locked by the manifest, so the template only matters for
    books entering the folder for the first time.  Page counts are deliberately
    forbidden: they are mutable metadata and must never influence a filename.
    """
    template = (template or '').strip()
    if not template:
        raise SyncError('The new-book filename template is empty.')
    if re.search(r'\{\s*#?pages\b', template, re.IGNORECASE):
        raise SyncError(
            'Page counts are not allowed in new-book filenames. Use {title} and {authors}; '
            'page-count changes must never rename a book.'
        )
    return template


# ---------------------------------------------------------------------------
# Atomic JSON and manifest handling
# ---------------------------------------------------------------------------

def _atomic_write_json(path, data):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    temporary = os.path.join(parent, TEMP_PREFIX + uuid.uuid4().hex + '.json.tmp')
    try:
        with open(temporary, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _read_json(path, label):
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, ValueError) as err:
        raise SyncError('The %s cannot be read: %s' % (label, err))
    if not isinstance(data, dict):
        raise SyncError('The %s has an invalid structure.' % label)
    return data


def new_manifest(library_id='', host=None):
    return {
        'version': MANIFEST_VERSION,
        'library_id': str(library_id or ''),
        'generation': 0,
        'backup_generation': 0,
        'host': host if host is not None else _hostname(),
        'updated_at': '',
        'books': {},
    }


def _normalise_entry(key, entry):
    if not isinstance(entry, dict):
        raise ManifestError('The manifest entry for %s is not an object.' % key)
    filename = entry.get('filename_locked') or entry.get('filename')
    if not _safe_basename(filename):
        raise ManifestError('The manifest contains an unsafe filename for %s: %r' % (key, filename))
    if entry.get('filename') and entry.get('filename_locked') and entry['filename'] != entry['filename_locked']:
        raise ManifestError('The manifest has conflicting filenames for %s.' % key)
    result = dict(entry)
    result['filename'] = filename
    result['filename_locked'] = filename
    state = result.get('state') or 'active'
    if state not in ('active', 'trashed'):
        raise ManifestError('The manifest has an unknown state for %s: %r' % (key, state))
    result['state'] = state
    result['format'] = str(
        result.get('format') or _extension(filename).lstrip('.')
    ).lower()
    return result


def _normalise_manifest(data):
    if not isinstance(data, dict) or not isinstance(data.get('books'), dict):
        raise ManifestError('The AnchorSync manifest has an invalid structure.')
    try:
        version = int(data.get('version', 1))
        generation = int(data.get('generation', 0) or 0)
        backup_generation = int(data.get('backup_generation', 0) or 0)
    except (TypeError, ValueError):
        raise ManifestError('The AnchorSync manifest has invalid version or generation fields.')
    if version < 1 or version > MANIFEST_VERSION:
        raise ManifestError('The AnchorSync manifest has an unsupported version: %s' % version)
    books = {}
    seen = {}
    for key, entry in data['books'].items():
        key = str(key)
        normalised = _normalise_entry(key, entry)
        name_key = _name_key(normalised['filename'])
        if name_key in seen:
            raise ManifestError(
                'The manifest locks the same filename twice: %s (%s and %s).'
                % (normalised['filename'], seen[name_key], key)
            )
        seen[name_key] = key
        books[key] = normalised
    result = dict(data)
    result['version'] = version
    result['generation'] = generation
    result['backup_generation'] = backup_generation
    result['library_id'] = str(data.get('library_id') or '')
    result['host'] = str(data.get('host') or '')
    result['updated_at'] = str(data.get('updated_at') or '')
    result['books'] = books
    return result


def manifest_exists(path):
    return os.path.isfile(path)


def load_manifest(path):
    """Load a v1 or v2 manifest.

    A missing manifest returns an empty v2 manifest.  Callers that must
    distinguish a fresh install from a lost manifest use
    :func:`manifest_exists` before calling this function.
    """
    if not os.path.isfile(path):
        return new_manifest()
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, ValueError) as err:
        raise ManifestError('The AnchorSync manifest cannot be read: %s' % err)
    return _normalise_manifest(data)


def _rotate_manifest_backups(path):
    if not os.path.isfile(path):
        return
    for index in range(MANIFEST_BACKUPS - 1, 0, -1):
        source = '%s.bak.%d' % (path, index)
        target = '%s.bak.%d' % (path, index + 1)
        if os.path.isfile(source):
            os.replace(source, target)
    shutil.copy2(path, path + '.bak.1')


def save_manifest(path, manifest, rotate=True):
    """Write a manifest atomically, keeping the previous generations.

    Callers own the generation counter.  Increment it before calling this
    function when the write represents a completed operation.
    """
    normalised = _normalise_manifest(manifest)
    normalised['version'] = MANIFEST_VERSION
    normalised['updated_at'] = _utc_now()
    previous_generation = 0
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as stream:
                previous = json.load(stream)
            previous_generation = int(previous.get('generation', 0) or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            previous_generation = 0
    normalised['backup_generation'] = previous_generation
    if not normalised.get('host'):
        normalised['host'] = _hostname()
    if rotate:
        _rotate_manifest_backups(path)
    _atomic_write_json(path, normalised)
    return normalised


# ---------------------------------------------------------------------------
# Single-writer lock
# ---------------------------------------------------------------------------

class SyncLock:
    """A non-blocking, process-wide lock held for the duration of an action."""

    def __init__(self, path):
        self.path = path
        self._stream = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False

    def acquire(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        stream = open(self.path, 'a+b')
        try:
            stream.seek(0)
            if stream.read(1) == b'':
                stream.seek(0)
                stream.write(b'anchorsync\n')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise LockError(
                'Another AnchorSync sync is already running. If no sync is running, '
                'close Calibre and try again (lock: %s).' % self.path
            )
        self._stream = stream
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(('pid=%s started=%s\n' % (os.getpid(), _utc_now())).encode('utf-8'))
            stream.flush()
        except OSError:
            pass

    def release(self):
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        stream.close()


def _lock_path(manifest_path, lock_path=None):
    return lock_path or (manifest_path + '.lock')


# ---------------------------------------------------------------------------
# Hashing and file scanning
# ---------------------------------------------------------------------------

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _target_hash(target, entry, stats):
    stat = os.stat(target)
    if entry and entry.get('sha256') and entry.get('size') == stat.st_size:
        same_time = (
            entry.get('mtime_ns') == stat.st_mtime_ns
            and entry.get('ctime_ns') == stat.st_ctime_ns
        )
        inode = entry.get('inode')
        same_inode = not inode or not stat.st_ino or int(inode) == int(stat.st_ino)
        if same_time and same_inode:
            stats['hash_cache_hits'] += 1
            return str(entry['sha256']), stat
    stats['target_hash_misses'] += 1
    return sha256_file(target), stat


def _scan_book_dir(book_dir):
    books = {}
    conflicts = []
    sidecars = {}
    if not os.path.isdir(book_dir):
        return {'books': books, 'conflicts': conflicts, 'sidecars': sidecars}
    for entry in os.scandir(book_dir):
        try:
            if entry.is_file() and _is_book_filename(entry.name):
                if is_conflict_filename(entry.name):
                    conflicts.append(entry.name)
                else:
                    books[entry.name] = entry.path
            elif entry.is_dir() and entry.name.lower().endswith('.sdr'):
                sidecars[entry.name] = entry.path
        except OSError:
            continue
    conflicts.sort()
    return {'books': books, 'conflicts': conflicts, 'sidecars': sidecars}


def _find_sidecar(sidecars, filename):
    """Return (sidecar name or None, ambiguous flag)."""
    exact = sidecar_name(filename)
    if exact in sidecars:
        return exact, False
    wanted = _name_key(exact)
    matches = [name for name in sidecars if _name_key(name) == wanted]
    if len(matches) == 1:
        return matches[0], False
    if len(matches) > 1:
        return None, True
    return None, False


def _compose_filename(book_dir, stem, extension, suffix_key=None, duplicate_index=0):
    suffix = ''
    if suffix_key:
        suffix += ' [%s]' % _short_uuid(suffix_key)
    if duplicate_index:
        suffix += ' (%d)' % duplicate_index
    extension = extension.lower()
    budget = MAX_FILENAME_BYTES - len(suffix.encode('utf-8')) - len(extension.encode('utf-8'))
    stem = _truncate_utf8(stem, max(16, budget)).rstrip(' .')
    if not stem:
        stem = 'Book'
    result = stem + suffix + extension
    while len(os.path.abspath(os.path.join(book_dir, result))) > MAX_FULL_PATH_LENGTH and len(stem) > 8:
        stem = stem[:-1].rstrip(' .')
        result = stem + suffix + extension
    if len(os.path.abspath(os.path.join(book_dir, result))) > MAX_FULL_PATH_LENGTH:
        raise SyncError(
            'The KOReader book folder path is too long for the new book name %r. '
            'Shorten the book folder path.' % result
        )
    return result


def _resolve_new_name(book_dir, stage_name, key, reserved):
    stem, extension = os.path.splitext(stage_name)
    candidate = _compose_filename(book_dir, stem, extension)
    if _name_key(candidate) not in reserved:
        return candidate
    candidate = _compose_filename(book_dir, stem, extension, suffix_key=key)
    if _name_key(candidate) not in reserved:
        return candidate
    for index in range(2, 1000):
        candidate = _compose_filename(book_dir, stem, extension, suffix_key=key, duplicate_index=index)
        if _name_key(candidate) not in reserved:
            return candidate
    raise SyncError('Cannot find a free filename for the exported book %r.' % stage_name)


# ---------------------------------------------------------------------------
# Staged exports
# ---------------------------------------------------------------------------

def _stage_files(stage_dir):
    grouped = {}
    if not os.path.isdir(stage_dir):
        raise SyncError('The temporary export folder does not exist: %s' % stage_dir)
    for entry in os.scandir(stage_dir):
        if not entry.is_file():
            continue
        match = STAGE_PREFIX.match(entry.name)
        if match is None:
            continue
        extension = _extension(entry.name)
        if extension not in SUPPORTED_EXTENSIONS:
            continue
        grouped.setdefault(int(match.group(1)), []).append((extension, entry.path, match.group(2)))
    return grouped


def discover_records(stage_dir, expected):
    """Return (records keyed by Calibre UUID, missing Calibre ids).

    EPUB is Preferred over PDF when both were exported for one book.
    """
    grouped = _stage_files(stage_dir)
    records = {}
    missing = []
    seen_uuids = {}
    for book_id, book_info in expected.items():
        candidates = sorted(
            grouped.get(int(book_id), ()),
            key=lambda item: (0 if item[0] == '.epub' else 1, item[2].casefold()),
        )
        if not candidates:
            missing.append(int(book_id))
            continue
        extension, stage_path, filename = candidates[0]
        _validate_stage_name(filename)
        key = str(book_info['uuid'])
        if key in seen_uuids:
            raise SyncError(
                'Two Calibre books have the same UUID (%s): %s and %s.'
                % (key, seen_uuids[key], book_id)
            )
        seen_uuids[key] = int(book_id)
        records[key] = {
            'id': int(book_id),
            'filename': filename,
            'format': extension.lstrip('.'),
            'stage_path': stage_path,
        }
    return records, missing


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def journal_path(manifest_path):
    return manifest_path + '.journal'


def journal_directory(manifest_path):
    return manifest_path + '.journal.d'


def _journal_save(manifest_path, journal):
    _atomic_write_json(journal_path(manifest_path), journal)


def _journal_begin(manifest_path, kind, generation_before, extra=None):
    path = journal_path(manifest_path)
    if os.path.exists(path):
        raise SyncError(
            'An unfinished previous operation exists. Run Sync or Cleanup once to recover it before continuing.'
        )
    # A journal directory without a journal file is an orphan left by a crash
    # while discarding a completed journal. It contains no rollback data.
    shutil.rmtree(journal_directory(manifest_path), ignore_errors=True)
    os.makedirs(journal_directory(manifest_path), exist_ok=True)
    journal = {
        'version': 1,
        'kind': kind,
        'run_id': uuid.uuid4().hex,
        'started_at': _utc_now(),
        'host': _hostname(),
        'generation_before': int(generation_before),
        'state': 'applying',
        'steps': [],
    }
    if extra:
        journal.update(extra)
    _journal_save(manifest_path, journal)
    return journal


def _journal_add_step(manifest_path, journal, step):
    step = dict(step)
    step['index'] = len(journal['steps'])
    step.setdefault('state', 'pending')
    journal['steps'].append(step)
    _journal_save(manifest_path, journal)
    return step


def _discard_journal(manifest_path):
    path = journal_path(manifest_path)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            # Leave the journal and its backups in place so the next recovery
            # can still use them.
            return
    shutil.rmtree(journal_directory(manifest_path), ignore_errors=True)


def _atomic_copy(source, target):
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    temporary = os.path.join(directory, TEMP_PREFIX + uuid.uuid4().hex + '.part')
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _restore_backup(backup, target):
    """Copy a journal backup back, working across volumes."""
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    temporary = os.path.join(directory, TEMP_PREFIX + uuid.uuid4().hex + '.rollback')
    try:
        shutil.copy2(backup, temporary)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    try:
        os.remove(backup)
    except OSError:
        pass


def _call_fault(fault, point, **details):
    if fault is not None:
        fault(point, **details)


def _apply_journaled_copy(manifest_path, journal, source, target, stage_hash,
                          target_hash_before=None, fault=None):
    target_dir = os.path.dirname(target)
    os.makedirs(target_dir, exist_ok=True)
    temporary = os.path.join(target_dir, TEMP_PREFIX + uuid.uuid4().hex + '.tmp')
    existed = os.path.isfile(target)
    backup = None
    if existed:
        backup = os.path.join(
            journal_directory(manifest_path),
            'backup',
            '%04d-%s' % (len(journal['steps']), _truncate_utf8(os.path.basename(target), 160)),
        )
        os.makedirs(os.path.dirname(backup), exist_ok=True)
    step = _journal_add_step(
        manifest_path,
        journal,
        {
            'op': 'copy',
            'source': os.path.abspath(source),
            'target': os.path.abspath(target),
            'temp': os.path.abspath(temporary),
            'backup': os.path.abspath(backup) if backup else None,
            'existed': existed,
            'stage_hash': stage_hash,
            'target_hash_before': target_hash_before,
            'state': 'pending',
        },
    )
    if existed:
        _atomic_copy(target, backup)
    _call_fault(fault, 'after_backup', target=target)
    shutil.copy2(source, temporary)
    _call_fault(fault, 'after_copy_temp', target=target)
    os.replace(temporary, target)
    _call_fault(fault, 'after_replace', target=target)
    step['state'] = 'done'
    _journal_save(manifest_path, journal)


def _rollback_copy_steps(manifest_path, journal):
    issues = []
    restored = 0
    removed = 0
    for step in reversed(journal.get('steps', [])):
        if step.get('op') != 'copy':
            continue
        target = step.get('target')
        temporary = step.get('temp')
        if temporary and os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError as err:
                issues.append('Cannot remove temporary file %s: %s' % (temporary, err))
        if not target:
            continue
        backup = step.get('backup')
        if step.get('existed'):
            if backup and os.path.isfile(backup):
                current_hash = None
                if os.path.exists(target):
                    try:
                        current_hash = sha256_file(target)
                    except OSError:
                        current_hash = None
                before_hash = step.get('target_hash_before')
                expected_hash = step.get('stage_hash')
                if (
                    current_hash is not None
                    and before_hash is not None
                    and current_hash == before_hash
                ):
                    # The target already has its pre-run content (the crash
                    # happened before the replace).  Leave it alone.
                    pass
                elif (
                    current_hash is not None
                    and before_hash is not None
                    and expected_hash is not None
                    and current_hash != expected_hash
                ):
                    issues.append(
                        'The target changed after the interrupted copy and was left in place: %s'
                        % target
                    )
                else:
                    try:
                        _restore_backup(backup, target)
                        restored += 1
                    except OSError as err:
                        issues.append('Cannot restore %s from the journal backup: %s' % (target, err))
            elif not os.path.exists(target):
                issues.append('The journal backup for %s is missing; the file cannot be restored.' % target)
        elif os.path.exists(target):
            expected_hash = step.get('stage_hash')
            try:
                still_ours = not expected_hash or sha256_file(target) == expected_hash
            except OSError:
                still_ours = False
            if still_ours:
                try:
                    os.remove(target)
                    removed += 1
                except OSError as err:
                    issues.append('Cannot remove the interrupted copy %s: %s' % (target, err))
            else:
                issues.append('The target changed after the interrupted copy and was left in place: %s' % target)
    return {'restored': restored, 'removed': removed, 'issues': issues}


def _rollback_move_steps(journal):
    issues = []
    restored = 0
    for step in reversed(journal.get('steps', [])):
        if step.get('op') != 'move':
            continue
        source = step.get('source')
        target = step.get('target')
        if not source or not target:
            continue
        if os.path.exists(target):
            if os.path.exists(source):
                issues.append('Cannot roll back a move because both locations exist: %s' % source)
                continue
            try:
                os.makedirs(os.path.dirname(source), exist_ok=True)
                shutil.move(target, source)
                restored += 1
            except OSError as err:
                issues.append('Cannot move %s back to %s: %s' % (target, source, err))
    return {'restored': restored, 'issues': issues}


def _preview_path_for_run(run_dir):
    return os.path.join(run_dir, 'preview.json')


def _mark_cleanup_preview_rolled_back(run_dir, report):
    preview_path = _preview_path_for_run(run_dir)
    if not os.path.isfile(preview_path):
        return
    try:
        preview = _read_json(preview_path, 'cleanup preview')
    except SyncError:
        return
    book_dir = preview.get('book_dir') or ''
    for item in preview.get('items', []):
        if book_dir and os.path.isfile(os.path.join(book_dir, item.get('filename', ''))):
            item['status'] = 'pending'
    preview['state'] = 'rolled_back'
    preview['recovery'] = report
    try:
        _atomic_write_json(preview_path, preview)
    except OSError:
        pass


def _mark_cleanup_preview_committed(run_dir):
    preview_path = _preview_path_for_run(run_dir)
    if not os.path.isfile(preview_path):
        return
    try:
        preview = _read_json(preview_path, 'cleanup preview')
    except SyncError:
        return
    if preview.get('state') not in ('complete', 'restored'):
        preview['state'] = 'complete'
    for item in preview.get('items', []):
        if item.get('status') == 'pending':
            item['status'] = 'moved'
    try:
        _atomic_write_json(preview_path, preview)
    except OSError:
        pass


def _recover_interrupted_run_locked(manifest_path):
    """Recover a journal while the caller holds the lock.

    Returns None when there is nothing to recover.  A journal whose manifest
    generation has advanced is considered committed; anything else is rolled
    back.  The journal is always removed after recovery.
    """
    path = journal_path(manifest_path)
    if not os.path.exists(path):
        shutil.rmtree(journal_directory(manifest_path), ignore_errors=True)
        return None
    try:
        journal = _read_json(path, 'operation journal')
    except SyncError:
        raise
    if not isinstance(journal, dict):
        raise SyncError('The operation journal has an invalid structure.')
    kind = journal.get('kind')
    if kind not in ('sync', 'cleanup'):
        raise SyncError('The operation journal has an unknown kind: %r' % (kind,))
    state = journal.get('state')
    if state in ('committed', 'rolled_back'):
        _discard_journal(manifest_path)
        return {'action': state, 'run_id': journal.get('run_id'), 'issues': []}

    manifest = None
    generation = None
    if os.path.isfile(manifest_path):
        try:
            manifest = load_manifest(manifest_path)
            generation = int(manifest.get('generation', 0) or 0)
        except ManifestError:
            manifest = None
    committed = (
        generation is not None
        and generation > int(journal.get('generation_before', 0) or 0)
    )
    if committed:
        run_dir = journal.get('run_dir')
        if run_dir and kind == 'cleanup':
            _mark_cleanup_preview_committed(run_dir)
        _discard_journal(manifest_path)
        return {
            'action': 'committed',
            'run_id': journal.get('run_id'),
            'kind': kind,
            'steps': len(journal.get('steps', [])),
            'issues': [],
        }

    if kind == 'cleanup':
        report = _rollback_move_steps(journal)
        run_dir = journal.get('run_dir')
        if run_dir:
            _mark_cleanup_preview_rolled_back(run_dir, report)
    else:
        report = _rollback_copy_steps(manifest_path, journal)
    _discard_journal(manifest_path)
    report.update({
        'action': 'rolled_back',
        'run_id': journal.get('run_id'),
        'kind': kind,
        'steps': len(journal.get('steps', [])),
    })
    return report


def recover_interrupted_run(manifest_path, lock_path=None):
    """Public recovery entry point; acquires the single-writer lock."""
    with SyncLock(_lock_path(manifest_path, lock_path)):
        return _recover_interrupted_run_locked(manifest_path)


def _settle_failed_journal(manifest_path, journal):
    """Recover or roll back a journal after an in-process failure."""
    generation = None
    if os.path.isfile(manifest_path):
        try:
            generation = int(load_manifest(manifest_path).get('generation', 0) or 0)
        except ManifestError:
            generation = None
    committed = (
        generation is not None
        and generation > int(journal.get('generation_before', 0) or 0)
    )
    if committed:
        report = {
            'action': 'committed',
            'run_id': journal.get('run_id'),
            'kind': journal.get('kind'),
            'steps': len(journal.get('steps', [])),
            'issues': [],
        }
        run_dir = journal.get('run_dir')
        if run_dir and journal.get('kind') == 'cleanup':
            _mark_cleanup_preview_committed(run_dir)
    elif journal.get('kind') == 'cleanup':
        report = _rollback_move_steps(journal)
        report['action'] = 'rolled_back'
        run_dir = journal.get('run_dir')
        if run_dir:
            _mark_cleanup_preview_rolled_back(run_dir, report)
    else:
        report = _rollback_copy_steps(manifest_path, journal)
        report['action'] = 'rolled_back'
    _discard_journal(manifest_path)
    return report


# ---------------------------------------------------------------------------
# Normal sync
# ---------------------------------------------------------------------------

def _source_fingerprint(info, stage_name):
    payload = {
        'stage_name': stage_name,
    }
    for key in ('title', 'authors', 'template'):
        value = info.get(key)
        if value is not None:
            payload[key] = value
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()


def _entry_filename(entry):
    return entry.get('filename_locked') or entry.get('filename')


def _stat_fields(stat):
    fields = {
        'size': stat.st_size,
        'mtime_ns': stat.st_mtime_ns,
        'ctime_ns': stat.st_ctime_ns,
    }
    inode = getattr(stat, 'st_ino', 0)
    if inode:
        fields['inode'] = int(inode)
    return fields


def reconcile(book_dir, stage_dir, expected, manifest_path, trash_dir='', *,
              template='', library_id='', library_dir='', dry_run=False,
              lock_path=None, host=None, fault=None):
    """Reconcile a staged Calibre export with the book folder.

    ``expected`` maps Calibre book ids to dictionaries containing at least
    ``uuid``.  Additional ``title``/``authors`` values are used for the
    manifest's source fingerprint.

    A normal run only copies or updates content under a filename locked by the
    manifest.  It never renames, deletes, or writes a sidecar.  ``dry_run=True``
    returns the same plan without changing anything.
    """
    book_dir = os.path.abspath(os.path.expanduser(book_dir))
    stage_dir = os.path.abspath(os.path.expanduser(stage_dir))
    validate_book_dir(book_dir, library_dir, trash_dir)
    if template:
        validate_template(template)
    # Do not create the book folder until the guards have passed and a copy is
    # actually required.  An aborted sync should leave no trace.
    expected = {int(book_id): info for book_id, info in expected.items()}

    manifest_path = os.path.abspath(os.path.expanduser(manifest_path))
    result = {
        'dry_run': bool(dry_run),
        'copied': 0,
        'new_files': 0,
        'updated': 0,
        'unchanged': 0,
        'adopted': 0,
        'renamed': 0,
        'deleted': 0,
        'sidecars_written': 0,
        'would_copy': 0,
        'missing': [],
        'name_drift': [],
        'format_changed': [],
        'removed_from_calibre': [],
        'unmanaged': [],
        'conflicts': [],
        'orphan_sidecars': [],
        'missing_from_folder': [],
        'reactivated': [],
        'export_complete': True,
        'hash_cache_hits': 0,
        'hash_cache_misses': 0,
        'stage_hashes': 0,
        'generation': 0,
        'recovery': None,
    }

    with SyncLock(_lock_path(manifest_path, lock_path)):
        if dry_run:
            if os.path.exists(journal_path(manifest_path)):
                raise SyncError(
                    'An unfinished previous operation exists. Run Sync once to recover it before previewing.'
                )
            recovery = None
        else:
            recovery = _recover_interrupted_run_locked(manifest_path)
        result['recovery'] = recovery

        manifest_file_exists = manifest_exists(manifest_path)
        manifest = load_manifest(manifest_path)
        if manifest.get('rebuild_incomplete'):
            raise ManifestError(
                'The manifest was rebuilt incompletely because existing files could not be matched. '
                'Nothing was changed. Resolve the unmatched files and run Rebuild manifest '
                '(read-only) again before syncing.'
            )
        source_version = int(manifest.get('version', 1) or 1)
        scan = _scan_book_dir(book_dir)

        if not manifest_file_exists and (scan['books'] or scan['conflicts']):
            raise ManifestError(
                'The AnchorSync manifest is missing, but the book folder already contains books. '
                'Nothing was changed. Restore or rebuild the manifest before syncing.'
            )

        if not expected:
            raise SyncError(
                'The Calibre library is empty. Nothing was synced; existing files were left untouched.'
            )

        records, missing = discover_records(stage_dir, expected)
        result['missing'] = sorted(missing)
        result['export_complete'] = not missing
        if not records:
            raise SyncError(
                'No EPUB or PDF files were staged. Nothing was changed; the existing book folder was left untouched.'
            )

        old_books = manifest.get('books', {})
        active_entries = {
            key: entry for key, entry in old_books.items()
            if (entry.get('state') or 'active') == 'active'
        }
        expected_count = len(expected)
        active_count = len(active_entries)
        if active_count and expected_count * 10 < active_count * 9:
            raise SyncError(
                'The Calibre library has %d books, more than 10%% below the manifest count of %d. '
                'Nothing was changed. If books were intentionally removed, run Cleanup first to '
                'acknowledge the removals, then sync again.'
                % (expected_count, active_count)
            )

        reserved = set(_name_key(_entry_filename(entry)) for entry in old_books.values())
        reserved.update(_name_key(name) for name in scan['books'])
        # A new book must not silently attach to an existing .sdr sidecar.
        # Reserve both supported extensions for every sidecar stem.
        for sidecar in scan['sidecars']:
            stem = os.path.splitext(sidecar)[0]
            for extension in SUPPORTED_EXTENSIONS:
                reserved.add(_name_key(stem + extension))

        resolved = {}
        drift = []
        format_changed = []
        format_changed_keys = set()
        reactivated = []
        for key in sorted(records, key=lambda item: str(item).casefold()):
            record = records[key]
            stage_name = record['filename']
            old = old_books.get(key)
            if old:
                locked = _entry_filename(old)
                resolved[key] = locked
                if stage_name != locked:
                    drift.append({
                        'uuid': key,
                        'id': record['id'],
                        'locked': locked,
                        'proposed': stage_name,
                    })
                if _extension(stage_name) != _extension(locked) or record['format'] != old.get('format'):
                    format_changed.append({
                        'uuid': key,
                        'id': record['id'],
                        'locked': locked,
                        'proposed': stage_name,
                        'proposed_format': record['format'],
                    })
                    format_changed_keys.add(key)
                if (
                    (old.get('state') or 'active') == 'trashed'
                    and key not in format_changed_keys
                ):
                    reactivated.append({'uuid': key, 'id': record['id'], 'filename': locked})
            else:
                name = _resolve_new_name(book_dir, stage_name, key, reserved)
                reserved.add(_name_key(name))
                resolved[key] = name

        result['name_drift'] = drift
        result['format_changed'] = format_changed
        result['reactivated'] = reactivated
        current_keys = set(records)

        for key in sorted(old_books, key=lambda item: str(item).casefold()):
            entry = old_books[key]
            if (entry.get('state') or 'active') != 'active' or key in current_keys:
                continue
            filename = _entry_filename(entry)
            sidecar, ambiguous = _find_sidecar(scan['sidecars'], filename)
            result['removed_from_calibre'].append({
                'uuid': key,
                'id': entry.get('id'),
                'filename': filename,
                'sidecar': sidecar,
                'sidecar_ambiguous': ambiguous,
                'file_exists': os.path.isfile(os.path.join(book_dir, filename)),
                'sidecar_exists': bool(sidecar),
            })

        stats = {
            'hash_cache_hits': 0,
            'target_hash_misses': 0,
            'stage_hashes': 0,
        }
        operations = []
        applied = {}
        copied = new_files = updated = unchanged = adopted = would_copy = 0

        for key in sorted(records, key=lambda item: str(item).casefold()):
            record = records[key]
            old = old_books.get(key)
            target_name = resolved[key]
            target = os.path.join(book_dir, target_name)
            try:
                stage_hash = sha256_file(record['stage_path'])
            except OSError as err:
                raise SyncError('Cannot read the staged export for book %s: %s' % (record['id'], err))
            stats['stage_hashes'] += 1
            record['stage_hash'] = stage_hash
            record['resolved_name'] = target_name
            record['action'] = 'unchanged'
            if key in format_changed_keys:
                record['action'] = 'format_changed'
                continue
            if os.path.exists(target):
                target_hash, target_stat = _target_hash(target, old, stats)
                if target_hash == stage_hash:
                    unchanged += 1
                    if old is None:
                        adopted += 1
                    record['action'] = 'unchanged'
                    applied[key] = (target_hash, target_stat)
                else:
                    updated += 1
                    would_copy += 1
                    record['action'] = 'update'
                    operations.append((key, record, target, stage_hash, target_hash))
            else:
                new_files += 1
                would_copy += 1
                record['action'] = 'new'
                operations.append((key, record, target, stage_hash, None))

        result['unchanged'] = unchanged
        result['adopted'] = adopted
        result['would_copy'] = would_copy
        result['hash_cache_hits'] = stats['hash_cache_hits']
        result['hash_cache_misses'] = stats['target_hash_misses']
        result['stage_hashes'] = stats['stage_hashes']

        journal = None
        try:
            if not dry_run and operations:
                journal = _journal_begin(
                    manifest_path,
                    'sync',
                    int(manifest.get('generation', 0) or 0),
                )
            if not dry_run:
                for key, record, target, stage_hash, target_hash_before in operations:
                    _call_fault(fault, 'before_copy', target=target)
                    _apply_journaled_copy(
                        manifest_path,
                        journal,
                        record['stage_path'],
                        target,
                        stage_hash,
                        target_hash_before=target_hash_before,
                        fault=fault,
                    )
                    try:
                        target_stat = os.stat(target)
                    except OSError as err:
                        raise SyncError('Cannot verify the copied book %s: %s' % (target, err))
                    applied[key] = (stage_hash, target_stat)
                    copied += 1

            new_books = {key: dict(entry) for key, entry in old_books.items()}
            changed = source_version < MANIFEST_VERSION
            for key in sorted(records, key=lambda item: str(item).casefold()):
                if key in format_changed_keys:
                    continue
                record = records[key]
                old = old_books.get(key)
                target_name = resolved[key]
                target = os.path.join(book_dir, target_name)
                if key in applied:
                    final_hash, target_stat = applied[key]
                else:
                    final_hash = record['stage_hash']
                    try:
                        target_stat = os.stat(target)
                    except OSError:
                        target_stat = None
                entry = dict(old) if old else {}
                before = dict(entry)
                entry.update({
                    'id': record['id'],
                    'filename': target_name,
                    'filename_locked': target_name,
                    'format': record['format'],
                    'sha256': final_hash,
                    'last_verified_hash': final_hash,
                    'state': 'active',
                    'source_fingerprint': _source_fingerprint(
                        expected.get(record['id'], {}), record['filename']
                    ),
                })
                if target_stat is not None:
                    entry.update(_stat_fields(target_stat))
                entry.pop('trashed_run', None)
                entry.pop('trashed_at', None)
                new_books[key] = entry
                if entry != before:
                    changed = True
            manifest['books'] = new_books
            if library_id and manifest.get('library_id') != str(library_id):
                manifest['library_id'] = str(library_id)
                changed = True
            if host is not None and manifest.get('host') != host:
                manifest['host'] = host
                changed = True
            if not manifest.get('host'):
                manifest['host'] = _hostname()
                changed = True

            if changed and not dry_run:
                manifest['generation'] = int(manifest.get('generation', 0) or 0) + 1
                save_manifest(manifest_path, manifest)
            result['generation'] = int(manifest.get('generation', 0) or 0)
            _call_fault(fault, 'after_manifest', manifest=manifest)
            if journal is not None:
                _discard_journal(manifest_path)
        except Exception:
            if journal is not None:
                try:
                    _settle_failed_journal(manifest_path, journal)
                except Exception as rollback_error:
                    raise SyncError(
                        'The sync failed and the rollback also failed: %s' % rollback_error
                    ) from rollback_error
            raise

        result['copied'] = copied if not dry_run else 0
        result['new_files'] = new_files if not dry_run else 0
        result['updated'] = updated if not dry_run else 0
        if dry_run:
            result['copied'] = 0
            result['new_files'] = 0
            result['updated'] = 0

        expected_names = {_name_key(resolved[key]) for key in records}
        expected_names.update(
            _name_key(item['filename']) for item in result['removed_from_calibre']
        )
        result['unmanaged'] = sorted(
            name for name in scan['books'] if _name_key(name) not in expected_names
        )
        result['conflicts'] = list(scan['conflicts'])
        book_stems = {_name_key(os.path.splitext(name)[0]) for name in scan['books']}
        result['orphan_sidecars'] = sorted(
            name for name in scan['sidecars']
            if _name_key(os.path.splitext(name)[0]) not in book_stems
        )

        missing_from_folder = []
        for key, entry in new_books.items():
            if (entry.get('state') or 'active') != 'active':
                continue
            if key in records and records[key]['action'] != 'format_changed':
                continue
            filename = _entry_filename(entry)
            if not os.path.isfile(os.path.join(book_dir, filename)):
                missing_from_folder.append(filename)
        result['missing_from_folder'] = sorted(missing_from_folder)
        result['sidecars_written'] = 0
        result['renamed'] = 0
        result['deleted'] = 0
        return result


# ---------------------------------------------------------------------------
# Cleanup preview and apply
# ---------------------------------------------------------------------------

def _preview_token(items):
    payload = [
        [
            item.get('uuid'),
            item.get('filename'),
            bool(item.get('book_exists')),
            item.get('sidecar') or '',
            bool(item.get('sidecar_ambiguous')),
        ]
        for item in items
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()


def _require_manifest(manifest_path):
    if not manifest_exists(manifest_path):
        raise ManifestError(
            'The AnchorSync manifest is missing. Cleanup cannot run without it; '
            'restore or rebuild the manifest first.'
        )
    return load_manifest(manifest_path)


def _build_cleanup_preview(book_dir, manifest, current_uuids):
    current = {str(value) for value in current_uuids}
    scan = _scan_book_dir(book_dir)
    items = []
    for key in sorted(manifest.get('books', {}), key=lambda item: str(item).casefold()):
        entry = manifest['books'][key]
        if (entry.get('state') or 'active') != 'active':
            continue
        if key in current:
            continue
        filename = _entry_filename(entry)
        sidecar, ambiguous = _find_sidecar(scan['sidecars'], filename)
        items.append({
            'uuid': key,
            'id': entry.get('id'),
            'filename': filename,
            'format': entry.get('format') or _extension(filename).lstrip('.'),
            'book_exists': os.path.isfile(os.path.join(book_dir, filename)),
            'sidecar': sidecar,
            'sidecar_exists': bool(sidecar),
            'sidecar_ambiguous': ambiguous,
        })
    items.sort(key=lambda item: (item['filename'].casefold(), str(item['uuid'])))
    return {
        'generated_at': _utc_now(),
        'book_dir': os.path.abspath(book_dir),
        'items': items,
        'total': len(items),
        'token': _preview_token(items),
    }


def cleanup_preview(book_dir, manifest_path, current_uuids, trash_dir, lock_path=None,
                    library_dir=''):
    """Return the preview for removed-from-Calibre books only."""
    book_dir = os.path.abspath(os.path.expanduser(book_dir))
    manifest_path = os.path.abspath(os.path.expanduser(manifest_path))
    trash_dir = os.path.abspath(os.path.expanduser(trash_dir)) if trash_dir else ''
    if not trash_dir:
        raise CleanupError('The deleted-book trash folder is not configured.')
    validate_book_dir(book_dir, library_dir, trash_dir)
    if not os.path.isdir(book_dir):
        raise CleanupError('The KOReader book folder does not exist: %s' % book_dir)
    with SyncLock(_lock_path(manifest_path, lock_path)):
        _recover_interrupted_run_locked(manifest_path)
        manifest = _require_manifest(manifest_path)
        return _build_cleanup_preview(book_dir, manifest, current_uuids)


def _move_path(source, target, label):
    if not os.path.exists(source):
        return False
    if os.path.exists(target):
        raise CleanupError('The trash destination already exists: %s' % target)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        shutil.move(source, target)
    except OSError as err:
        raise CleanupError('Cannot move %s to the trash: %s' % (label, err))
    return True


def _cleanup_run_id():
    return time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]


def apply_cleanup(book_dir, manifest_path, trash_dir, current_uuids,
                  preview_token=None, confirm=False, confirm_large=False,
                  lock_path=None, fault=None, library_dir=''):
    """Move removed-from-Calibre books and their sidecars to a trash run."""
    book_dir = os.path.abspath(os.path.expanduser(book_dir))
    manifest_path = os.path.abspath(os.path.expanduser(manifest_path))
    trash_dir = os.path.abspath(os.path.expanduser(trash_dir)) if trash_dir else ''
    if not trash_dir:
        raise CleanupError('The deleted-book trash folder is not configured.')
    validate_book_dir(book_dir, library_dir, trash_dir)
    if not os.path.isdir(book_dir):
        raise CleanupError('The KOReader book folder does not exist: %s' % book_dir)
    if not confirm:
        raise CleanupError('Cleanup requires explicit confirmation.')

    with SyncLock(_lock_path(manifest_path, lock_path)):
        _recover_interrupted_run_locked(manifest_path)
        manifest = _require_manifest(manifest_path)
        preview = _build_cleanup_preview(book_dir, manifest, current_uuids)
        if preview_token is not None and preview_token != preview['token']:
            raise CleanupError(
                'The cleanup preview is out of date. Run Cleanup again and review the current list.'
            )
        items = preview['items']
        if not items:
            return {
                'run_id': None,
                'run_dir': None,
                'total': 0,
                'moved': 0,
                'failed': [],
                'trash_dir': trash_dir,
            }
        if len(items) > CLEANUP_CONFIRM_THRESHOLD and not confirm_large:
            raise CleanupError(
                'This cleanup would move %d books. Confirm the large cleanup explicitly before continuing.'
                % len(items)
            )

        os.makedirs(trash_dir, exist_ok=True)
        run_id = _cleanup_run_id()
        run_dir = os.path.join(trash_dir, run_id)
        while os.path.exists(run_dir):
            run_id = _cleanup_run_id()
            run_dir = os.path.join(trash_dir, run_id)
        os.makedirs(os.path.join(run_dir, 'books'), exist_ok=True)
        os.makedirs(os.path.join(run_dir, 'sidecars'), exist_ok=True)

        for item in items:
            item['status'] = 'pending'
            item['book_moved'] = False
            item['sidecar_moved'] = False
        stored_preview = {
            'version': 1,
            'run_id': run_id,
            'created_at': _utc_now(),
            'host': _hostname(),
            'library_id': manifest.get('library_id') or '',
            'manifest_generation': int(manifest.get('generation', 0) or 0),
            'book_dir': book_dir,
            'trash_dir': trash_dir,
            'token': preview['token'],
            'state': 'applying',
            'items': items,
        }
        preview_path = _preview_path_for_run(run_dir)
        try:
            _atomic_write_json(preview_path, stored_preview)
        except OSError as err:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise CleanupError('Cannot write the trash preview: %s' % err)

        journal = None
        moved_items = []
        try:
            has_moves = any(
                item['book_exists'] or (item['sidecar_exists'] and not item['sidecar_ambiguous'])
                for item in items
            )
            if has_moves:
                journal = _journal_begin(
                    manifest_path,
                    'cleanup',
                    int(manifest.get('generation', 0) or 0),
                    {'run_id': run_id, 'run_dir': run_dir},
                )
            for item in items:
                filename = item['filename']
                if item['book_exists']:
                    source = os.path.join(book_dir, filename)
                    target = os.path.join(run_dir, 'books', filename)
                    if journal is not None:
                        _journal_add_step(
                            manifest_path,
                            journal,
                            {
                                'op': 'move',
                                'source': source,
                                'target': target,
                                'label': 'book',
                                'item_uuid': item['uuid'],
                            },
                        )
                    _call_fault(fault, 'cleanup_before_book_move', item=item)
                    item['book_moved'] = _move_path(source, target, filename)
                    _call_fault(fault, 'cleanup_after_book_move', item=item)
                if item['sidecar_exists'] and not item['sidecar_ambiguous']:
                    source = os.path.join(book_dir, item['sidecar'])
                    target = os.path.join(run_dir, 'sidecars', item['sidecar'])
                    if journal is not None:
                        _journal_add_step(
                            manifest_path,
                            journal,
                            {
                                'op': 'move',
                                'source': source,
                                'target': target,
                                'label': 'sidecar',
                                'item_uuid': item['uuid'],
                            },
                        )
                    _call_fault(fault, 'cleanup_before_sidecar_move', item=item)
                    item['sidecar_moved'] = _move_path(source, target, item['sidecar'])
                    _call_fault(fault, 'cleanup_after_sidecar_move', item=item)
                item['status'] = 'moved'
                moved_items.append(item)
                _atomic_write_json(preview_path, stored_preview)

            for item in items:
                entry = manifest['books'].get(item['uuid'])
                if entry is None:
                    continue
                entry['state'] = 'trashed'
                entry['trashed_run'] = run_id
                entry['trashed_at'] = _utc_now()
                entry['filename'] = item['filename']
                entry['filename_locked'] = item['filename']
            manifest['generation'] = int(manifest.get('generation', 0) or 0) + 1
            save_manifest(manifest_path, manifest)
            _call_fault(fault, 'cleanup_after_manifest', manifest=manifest)
            stored_preview['state'] = 'complete'
            stored_preview['completed_at'] = _utc_now()
            _atomic_write_json(preview_path, stored_preview)
            if journal is not None:
                _discard_journal(manifest_path)
        except Exception as err:
            committed = False
            settled_action = None
            if journal is not None:
                try:
                    report = _settle_failed_journal(manifest_path, journal)
                except Exception as rollback_error:
                    raise CleanupError(
                        'Cleanup failed and the rollback also failed: %s' % rollback_error
                    ) from rollback_error
                committed = report.get('action') == 'committed'
                settled_action = report.get('action')
                if report.get('issues'):
                    raise CleanupError(
                        'Cleanup failed and the rollback left issues: %s'
                        % '; '.join(report['issues'])
                    ) from err
            # Recovery writes the most accurate item states to preview.json.
            # Reload it before recording the error so those states survive.
            try:
                if os.path.isfile(preview_path):
                    loaded_preview = _read_json(preview_path, 'cleanup preview')
                    if isinstance(loaded_preview, dict):
                        stored_preview = loaded_preview
            except SyncError:
                pass
            if committed:
                stored_preview['state'] = 'complete'
                stored_preview['completed_at'] = _utc_now()
            elif settled_action == 'rolled_back':
                stored_preview['state'] = 'rolled_back'
            else:
                stored_preview['state'] = 'failed'
            stored_preview['error'] = str(err)
            try:
                _atomic_write_json(preview_path, stored_preview)
            except OSError:
                pass
            if committed:
                raise CleanupError(
                    'Cleanup moved the approved books to the trash, but could not finish '
                    'writing the run report: %s' % err
                ) from err
            if isinstance(err, CleanupError):
                raise
            raise CleanupError('Cleanup failed: %s' % err) from err

        return {
            'run_id': run_id,
            'run_dir': run_dir,
            'total': len(items),
            'moved': len(moved_items),
            'books': sum(1 for item in moved_items if item['book_moved']),
            'sidecars': sum(1 for item in moved_items if item['sidecar_moved']),
            'failed': [],
            'trash_dir': trash_dir,
        }


# ---------------------------------------------------------------------------
# Trash listing and restore
# ---------------------------------------------------------------------------

def list_trash_runs(trash_dir):
    trash_dir = os.path.abspath(os.path.expanduser(trash_dir)) if trash_dir else ''
    if not trash_dir or not os.path.isdir(trash_dir):
        return []
    runs = []
    for entry in os.scandir(trash_dir):
        if not entry.is_dir():
            continue
        preview_path = os.path.join(entry.path, 'preview.json')
        if not os.path.isfile(preview_path):
            continue
        try:
            preview = _read_json(preview_path, 'cleanup preview')
            items = preview.get('items', [])
            if not isinstance(items, list):
                continue
            restored = sum(1 for item in items if item.get('status') == 'restored')
            # Base remaining on what is actually on disk.  A rolled-back run
            # has a preview but no files left in trash, and must not be offered
            # as a restore candidate.
            run_books = os.path.join(entry.path, 'books')
            run_sidecars = os.path.join(entry.path, 'sidecars')
            remaining = 0
            for item in items:
                if item.get('status') == 'restored' or item.get('restored'):
                    continue
                filename = item.get('filename')
                sidecar = item.get('sidecar')
                if filename and os.path.exists(os.path.join(run_books, filename)):
                    remaining += 1
                elif sidecar and os.path.exists(os.path.join(run_sidecars, sidecar)):
                    remaining += 1
            runs.append({
                'run_id': preview.get('run_id') or entry.name,
                'path': entry.path,
                'created_at': preview.get('created_at') or '',
                'state': preview.get('state') or 'unknown',
                'total': len(items),
                'restored': restored,
                'remaining': remaining,
                'library_id': preview.get('library_id') or '',
                'book_dir': preview.get('book_dir') or '',
            })
        except SyncError:
            runs.append({
                'run_id': entry.name,
                'path': entry.path,
                'created_at': '',
                'state': 'invalid',
                'total': 0,
                'restored': 0,
                'remaining': 0,
                'error': 'preview.json cannot be read',
            })
    runs.sort(key=lambda run: (run.get('created_at') or '', run.get('run_id') or ''), reverse=True)
    return runs


def _safe_run_dir(trash_dir, run_id):
    if not run_id or not _safe_basename(run_id):
        raise RestoreError('Invalid trash run id.')
    trash_root = os.path.abspath(trash_dir)
    run_dir = os.path.abspath(os.path.join(trash_root, run_id))
    try:
        if os.path.commonpath([trash_root, run_dir]) != trash_root:
            raise RestoreError('The trash run is outside the trash folder.')
    except ValueError:
        raise RestoreError('The trash run is outside the trash folder.')
    if not os.path.isfile(_preview_path_for_run(run_dir)):
        raise RestoreError('The trash run has no preview.json: %s' % run_id)
    return run_dir


def _restore_item(item, run_dir, book_dir):
    filename = item.get('filename')
    if not _safe_basename(filename):
        return {'status': 'failed', 'reason': 'unsafe filename'}
    sidecar = item.get('sidecar') or sidecar_name(filename)
    if not _safe_basename(sidecar):
        return {'status': 'failed', 'reason': 'unsafe sidecar name'}
    book_source = os.path.join(run_dir, 'books', filename)
    book_target = os.path.join(book_dir, filename)
    sidecar_source = os.path.join(run_dir, 'sidecars', sidecar)
    sidecar_target = os.path.join(book_dir, sidecar)
    book_source_exists = os.path.exists(book_source)
    sidecar_source_exists = os.path.exists(sidecar_source)
    book_target_exists = os.path.exists(book_target)
    sidecar_target_exists = os.path.exists(sidecar_target)

    if book_source_exists and book_target_exists:
        return {'status': 'conflict', 'reason': 'book destination is occupied'}
    if sidecar_source_exists and sidecar_target_exists:
        return {'status': 'conflict', 'reason': 'sidecar destination is occupied'}
    if not book_source_exists and not sidecar_source_exists:
        if book_target_exists or sidecar_target_exists:
            return {'status': 'already_restored', 'reason': 'destination already contains the book'}
        return {'status': 'missing', 'reason': 'the trash run has neither the book nor its sidecar'}

    book_moved = False
    sidecar_moved = False
    try:
        if book_source_exists:
            os.makedirs(book_dir, exist_ok=True)
            shutil.move(book_source, book_target)
            book_moved = True
        if sidecar_source_exists:
            os.makedirs(book_dir, exist_ok=True)
            shutil.move(sidecar_source, sidecar_target)
            sidecar_moved = True
    except OSError as err:
        if book_moved and not sidecar_moved:
            try:
                shutil.move(book_target, book_source)
                book_moved = False
            except OSError:
                pass
        return {'status': 'failed', 'reason': str(err), 'book_moved': book_moved, 'sidecar_moved': sidecar_moved}
    return {
        'status': 'restored',
        'book_moved': book_moved,
        'sidecar_moved': sidecar_moved,
    }


def restore_trash_run(trash_dir, run_id, book_dir, manifest_path, *,
                      items=None, confirm=False, lock_path=None, library_dir=''):
    """Restore a cleanup run, but never overwrite an occupied destination."""
    if not confirm:
        raise RestoreError('Restore requires explicit confirmation.')
    trash_dir = os.path.abspath(os.path.expanduser(trash_dir)) if trash_dir else ''
    book_dir = os.path.abspath(os.path.expanduser(book_dir))
    manifest_path = os.path.abspath(os.path.expanduser(manifest_path))
    if not trash_dir or not os.path.isdir(trash_dir):
        raise RestoreError('The deleted-book trash folder does not exist: %s' % trash_dir)
    validate_book_dir(book_dir, library_dir)
    run_dir = _safe_run_dir(trash_dir, run_id)
    selected = None
    if items is not None:
        selected = {str(value) for value in items}

    with SyncLock(_lock_path(manifest_path, lock_path)):
        _recover_interrupted_run_locked(manifest_path)
        preview = _read_json(_preview_path_for_run(run_dir), 'cleanup preview')
        preview_items = preview.get('items', [])
        if not isinstance(preview_items, list):
            raise RestoreError('The cleanup preview has an invalid structure.')
        manifest = _require_manifest(manifest_path)
        restored = []
        conflicts = []
        missing = []
        failed = []
        skipped = []
        for item in sorted(preview_items, key=lambda value: (value.get('filename') or '').casefold()):
            if selected is not None:
                if item.get('uuid') not in selected and item.get('filename') not in selected:
                    continue
            outcome = _restore_item(item, run_dir, book_dir)
            status = outcome['status']
            if status == 'conflict':
                item['restore_status'] = 'conflict'
                item['restore_error'] = outcome.get('reason')
                conflicts.append({'filename': item.get('filename'), 'reason': outcome.get('reason')})
                continue
            if status == 'failed':
                item['restore_status'] = 'failed'
                item['restore_error'] = outcome.get('reason')
                failed.append({'filename': item.get('filename'), 'reason': outcome.get('reason')})
                continue
            if status == 'missing':
                item['restore_status'] = 'missing'
                missing.append({'filename': item.get('filename'), 'reason': outcome.get('reason')})
                continue
            item['status'] = 'restored'
            item['restored'] = True
            item['restored_at'] = _utc_now()
            item['restore_status'] = 'restored'
            restored.append(item.get('filename'))
            entry = manifest.get('books', {}).get(str(item.get('uuid')))
            if entry is None:
                entry = {
                    'id': item.get('id'),
                    'filename': item.get('filename'),
                    'filename_locked': item.get('filename'),
                    'format': item.get('format') or _extension(item.get('filename') or '').lstrip('.'),
                    'state': 'active',
                }
                manifest.setdefault('books', {})[str(item.get('uuid'))] = entry
            entry['state'] = 'active'
            entry['filename'] = item.get('filename')
            entry['filename_locked'] = item.get('filename')
            entry.pop('trashed_run', None)
            entry.pop('trashed_at', None)
            book_path = os.path.join(book_dir, item.get('filename') or '')
            if os.path.isfile(book_path):
                try:
                    stat = os.stat(book_path)
                    entry.update(_stat_fields(stat))
                    entry['sha256'] = sha256_file(book_path)
                    entry['last_verified_hash'] = entry['sha256']
                except OSError:
                    pass

        if restored:
            manifest['generation'] = int(manifest.get('generation', 0) or 0) + 1
            save_manifest(manifest_path, manifest)
        remaining = [
            item for item in preview_items
            if item.get('status') != 'restored' and not item.get('restored')
        ]
        preview['state'] = 'restored' if (restored and not remaining) else (
            'restore-incomplete' if (conflicts or failed or missing) else preview.get('state')
        )
        preview['last_restore_at'] = _utc_now()
        _atomic_write_json(_preview_path_for_run(run_dir), preview)
        return {
            'run_id': run_id,
            'run_dir': run_dir,
            'restored': restored,
            'conflicts': conflicts,
            'missing': missing,
            'failed': failed,
            'skipped': skipped,
            'remaining': [item.get('filename') for item in remaining],
        }


# ---------------------------------------------------------------------------
# Manifest rebuild (read-only with respect to the book folder)
# ---------------------------------------------------------------------------

def rebuild_manifest(book_dir, manifest_path, expected, proposed_names=None,
                     stage_dir=None, library_id='', dry_run=False,
                     replace_corrupt=False, lock_path=None, library_dir=''):
    """Adopt existing folder files into a manifest without moving anything.

    This never deletes a file or a manifest entry.  A missing or corrupt
    manifest can be replaced only when ``replace_corrupt`` is explicitly set;
    the previous file is still rotated to ``.bak.1``.
    """
    book_dir = os.path.abspath(os.path.expanduser(book_dir))
    manifest_path = os.path.abspath(os.path.expanduser(manifest_path))
    validate_book_dir(book_dir, library_dir)
    if not os.path.isdir(book_dir):
        raise SyncError('The KOReader book folder does not exist: %s' % book_dir)

    proposed = {str(key): value for key, value in (proposed_names or {}).items()}
    if stage_dir:
        records, _missing = discover_records(stage_dir, expected)
        if expected and not records:
            raise SyncError(
                'No staged export was available. The manifest rebuild was aborted; nothing was changed.'
            )
        proposed.update({key: record['filename'] for key, record in records.items()})

    with SyncLock(_lock_path(manifest_path, lock_path)):
        _recover_interrupted_run_locked(manifest_path)
        manifest_present = manifest_exists(manifest_path)
        manifest_was_missing = not manifest_present
        replaced_corrupt = False
        if manifest_present:
            try:
                manifest = load_manifest(manifest_path)
            except ManifestError:
                if not replace_corrupt:
                    raise
                manifest = new_manifest(library_id=library_id)
                replaced_corrupt = True
        else:
            manifest = new_manifest(library_id=library_id)
        scan = _scan_book_dir(book_dir)
        available = {name: path for name, path in scan['books'].items()}
        reserved = set(_name_key(_entry_filename(entry)) for entry in manifest.get('books', {}).values())
        matched = []
        unmatched = []
        ambiguous = []
        changed = replaced_corrupt or int(manifest.get('version', 1) or 1) < MANIFEST_VERSION
        for book_id, info in sorted(expected.items(), key=lambda item: int(item[0])):
            key = str(info['uuid'])
            if key in manifest.get('books', {}):
                continue
            candidates = []
            proposed_name = proposed.get(key)
            if proposed_name:
                wanted = _name_key(proposed_name)
                candidates = [name for name in available if _name_key(name) == wanted and _name_key(name) not in reserved]
            if not candidates:
                suffix = _name_key(' [%s]' % _short_uuid(key))
                candidates = [
                    name for name in available
                    if suffix in _name_key(os.path.splitext(name)[0]) and _name_key(name) not in reserved
                ]
            if len(candidates) > 1:
                ambiguous.append({'uuid': key, 'id': int(book_id), 'candidates': sorted(candidates)})
                continue
            if not candidates:
                unmatched.append({'uuid': key, 'id': int(book_id), 'proposed': proposed_name or ''})
                continue
            filename = candidates[0]
            path = available[filename]
            try:
                stat = os.stat(path)
                digest = sha256_file(path)
            except OSError as err:
                unmatched.append({'uuid': key, 'id': int(book_id), 'proposed': filename, 'error': str(err)})
                continue
            entry = {
                'id': int(book_id),
                'filename': filename,
                'filename_locked': filename,
                'format': _extension(filename).lstrip('.'),
                'sha256': digest,
                'last_verified_hash': digest,
                'state': 'active',
            }
            entry.update(_stat_fields(stat))
            manifest.setdefault('books', {})[key] = entry
            reserved.add(_name_key(filename))
            matched.append({'uuid': key, 'id': int(book_id), 'filename': filename})
            changed = True

        extra_books = sorted(
            name for name in scan['books'] if _name_key(name) not in reserved
        )
        recovery = (
            manifest_was_missing
            or replaced_corrupt
            or bool(manifest.get('rebuild_incomplete'))
        )
        incomplete = recovery and bool(extra_books) and bool(unmatched or ambiguous)
        if incomplete:
            if not manifest.get('rebuild_incomplete'):
                manifest['rebuild_incomplete'] = True
                changed = True
        elif manifest.pop('rebuild_incomplete', None) is not None:
            changed = True

        if library_id and manifest.get('library_id') != str(library_id):
            manifest['library_id'] = str(library_id)
            changed = True
        if not manifest.get('host'):
            manifest['host'] = _hostname()
            changed = True
        if changed and not dry_run:
            manifest['version'] = MANIFEST_VERSION
            manifest['generation'] = int(manifest.get('generation', 0) or 0) + 1
            save_manifest(manifest_path, manifest)
        return {
            'matched': matched,
            'unmatched': unmatched,
            'ambiguous': ambiguous,
            'extra_books': extra_books,
            'incomplete': incomplete,
            'preserved_entries': len(manifest.get('books', {})) - len(matched),
            'replaced_corrupt': replaced_corrupt,
            'dry_run': bool(dry_run),
            'generation': int(manifest.get('generation', 0) or 0),
        }
