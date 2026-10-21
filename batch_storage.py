"""Delete one registered evaluation and all website-owned copies of its files."""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import shutil
import sqlite3
import time
from uuid import uuid4

import run_imports
from run_bundle import inspect_run


class StorageError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def managed_folder(path, roots):
    """Require a direct child of a configured batch directory, never its root."""
    path = Path(path)
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
        raise StorageError(409, 'A batch contains a filesystem link; no files were deleted.')
    absolute = path.resolve()
    if not any(absolute.parent == Path(root).resolve() and absolute != Path(root).resolve() for root in roots):
        raise StorageError(409, 'The batch path is outside website-managed storage.')
    return absolute


def file_state(folder):
    files = []
    for path in sorted(folder.rglob('*')):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise StorageError(409, 'A batch contains a filesystem link; no files were deleted.')
        if path.is_file():
            stat = path.stat()
            files.append((path.relative_to(folder).as_posix(), stat.st_size, stat.st_mtime_ns))
    return files


def plan(db, assignment, roots):
    if not isinstance(assignment, str):
        raise StorageError(400, 'Choose an evaluation batch.')
    row = db.execute('SELECT * FROM assignments WHERE id=?', (assignment,)).fetchone()
    if not row:
        raise StorageError(404, 'Evaluation batch not found.')
    candidates = set()
    registered = Path(row['bundle'])
    if registered.is_dir():
        candidates.add(managed_folder(registered, roots))
    for folder in run_imports.discover_runs(roots):
        try:
            if inspect_run(folder)['id'] == assignment:
                candidates.add(managed_folder(folder, roots))
        except (ValueError, OSError, KeyError, TypeError):
            continue
    # Never remove a directory that another registered batch still references.
    others = [Path(item[0]).resolve() for item in db.execute('SELECT bundle FROM assignments WHERE id!=?', (assignment,))]
    folders, size = [], 0
    for folder in sorted(candidates):
        folder = managed_folder(folder, roots)
        if any(other == folder or other.is_relative_to(folder) for other in others):
            raise StorageError(409, 'Another batch shares these files; deletion was stopped.')
        files = file_state(folder)
        size += sum(item[1] for item in files)
        folders.append((str(folder), files))
    findings = [tuple(item) for item in db.execute('''SELECT f.id,f.version,f.updated FROM findings f
        JOIN submissions s ON s.id=f.submission_id WHERE s.assignment_id=? ORDER BY f.id''', (assignment,))]
    submissions = [tuple(item) for item in db.execute('SELECT id,pdf FROM submissions WHERE assignment_id=? ORDER BY id', (assignment,))]
    visibility = db.execute('SELECT visible,version FROM batch_visibility WHERE assignment_id=?', (assignment,)).fetchone()
    payload = {'assignment': dict(row), 'folders': folders, 'findings': findings,
               'submissions': submissions, 'visibility': tuple(visibility) if visibility else None}
    token = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return {'assignment': assignment, 'label': row['label'], 'students': len(submissions),
            'findings': len(findings), 'bytes': size, 'copies': len(folders), 'token': token,
            '_folders': [Path(item[0]) for item in folders]}


def preview(db, assignment, roots):
    with run_imports.STORAGE_LOCK:
        return {key: value for key, value in plan(db, assignment, roots).items() if not key.startswith('_')}


def remove_tree(path, parent):
    """Check the final absolute target before a recursive filesystem deletion."""
    path, parent = Path(path), Path(parent).resolve()
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)() or path.resolve().parent != parent:
        raise StorageError(409, 'Invalid deletion staging directory.')
    file_state(path)
    shutil.rmtree(path)


def recover(db, data_root, roots):
    """Restore an interrupted transaction, or finish its committed file removal."""
    with run_imports.STORAGE_LOCK:
        staging_root = Path(data_root).resolve() / '.deleting'
        pending = 0
        if not staging_root.is_dir():
            return {'pending': 0}
        for job in staging_root.iterdir():
            if not job.is_dir() or job.is_symlink():
                continue
            try:
                if job.resolve().parent != staging_root or getattr(job, 'is_junction', lambda: False)():
                    raise StorageError(409, 'Invalid deletion staging directory.')
                journal = json.loads((job / 'plan.json').read_text(encoding='utf-8'))
                exists = db.execute('SELECT 1 FROM assignments WHERE id=?', (journal['assignment'],)).fetchone()
                if exists:
                    for index, source in enumerate(journal['folders']):
                        source = managed_folder(source, roots)
                        held = job / str(index)
                        if held.exists():
                            if source.exists():
                                raise StorageError(409, 'A recovered batch conflicts with existing files.')
                            held.rename(source)
                remove_tree(job, staging_root)
            except (OSError, ValueError, KeyError, TypeError, StorageError):
                pending += 1
        return {'pending': pending}


def delete(db, user, data, data_root, roots):
    if user['role'] != 'instructor':
        raise StorageError(403, 'Only instructors can delete a batch.')
    if data.get('confirmation') != 'DELETE' or not isinstance(data.get('token'), str):
        raise StorageError(400, 'Type DELETE to confirm permanent batch deletion.')
    with run_imports.STORAGE_LOCK:
        db.execute('BEGIN IMMEDIATE')
        current = plan(db, data.get('assignment'), roots)
        if data['token'] != current['token']:
            raise StorageError(409, 'This batch changed. Open its deletion preview again before deleting.')
        staging_root = Path(data_root).resolve() / '.deleting'
        staging_root.mkdir(parents=True, exist_ok=True)
        job = staging_root / uuid4().hex
        job.mkdir()
        folders = current['_folders']
        (job / 'plan.json').write_text(json.dumps({'assignment': current['assignment'],
            'folders': [str(folder) for folder in folders]}), encoding='utf-8')
        committed = False
        try:
            for index, folder in enumerate(folders):
                managed_folder(folder, roots)
                folder.rename(job / str(index))
            args = (current['assignment'],)
            db.execute('''DELETE FROM audit WHERE finding IN (SELECT f.id FROM findings f
                JOIN submissions s ON s.id=f.submission_id WHERE s.assignment_id=?)''', args)
            db.execute('DELETE FROM findings WHERE submission_id IN (SELECT id FROM submissions WHERE assignment_id=?)', args)
            db.execute('DELETE FROM submissions WHERE assignment_id=?', args)
            db.execute('DELETE FROM batch_visibility WHERE assignment_id=?', args)
            db.execute('DELETE FROM assignments WHERE id=?', args)
            db.execute('INSERT INTO audit(actor,finding,action,payload,at) VALUES (?,?,?,?,?)',
                (user['id'], None, 'batch_deleted', json.dumps({key: current[key] for key in
                 ('assignment','label','students','findings','bytes')}), time.time()))
            db.commit()
            committed = True
        except Exception:
            db.rollback()
            for index, folder in enumerate(folders):
                held = job / str(index)
                if held.exists() and not folder.exists():
                    held.rename(managed_folder(folder, roots))
            remove_tree(job, staging_root)
            raise
        try:
            remove_tree(job, staging_root)
            pending = False
        except (OSError, StorageError):
            pending = True
        try:
            db.execute('VACUUM')
        except sqlite3.Error:
            pass  # Freed database pages remain reusable if compaction is busy.
        return {'deleted': committed, 'label': current['label'], 'bytes': current['bytes'],
                'cleanup_pending': pending}
