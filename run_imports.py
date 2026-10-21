"""Discover unchanged evaluation folders and register them in private storage."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import json
import re
import shutil
import stat
import tempfile
import time
import zipfile

import fitz
from run_bundle import inspect_run, run_files


def discover_runs(roots):
    found, seen = [], set()
    for root in roots:
        root = Path(root)
        if not root.is_dir() or root.is_symlink():
            continue
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or folder.is_symlink() or folder.name.startswith("."):
                continue
            if not ((folder / "results.json").exists() or (folder / "manifest.json").exists()
                    or folder.name.startswith("evaluation_")):
                continue
            identity = str(folder.resolve())
            if identity not in seen:
                seen.add(identity)
                found.append(folder)
    return found


def cache_run(info, data_root):
    """Own a persistent copy, independent of Git checkouts and inbox locations."""
    cache_root = Path(data_root).resolve() / "evaluations"
    cache_root.mkdir(parents=True, exist_ok=True)
    destination = cache_root / info["id"]
    if destination.exists():
        cached = inspect_run(destination)
        if cached["fingerprint"] != info["fingerprint"]:
            raise ValueError("This import identity already belongs to a different evaluation")
    else:
        run_files(info["root"])
        with tempfile.TemporaryDirectory(prefix=".cache-", dir=cache_root) as temporary:
            staging = Path(temporary) / info["root"].name
            shutil.copytree(info["root"], staging)
            inspect_run(staging)
            try:
                staging.rename(destination)
            except OSError:
                if not destination.exists() or inspect_run(destination)["fingerprint"] != info["fingerprint"]:
                    raise
        cached = inspect_run(destination)
    cached["label"] = info["label"]
    return cached


def import_run(folder, data_root, connect, create_user):
    info = inspect_run(Path(folder))
    with connect() as db:
        old = db.execute("SELECT bundle FROM assignments WHERE id=?", (info["id"],)).fetchone()
    if old:
        old_folder = Path(old["bundle"])
        if old_folder.is_dir() and inspect_run(old_folder)["fingerprint"] != info["fingerprint"]:
            raise ValueError("An already imported evaluation changed; use a new completed run")
        if not old_folder.is_dir() and info["id"] != "evaluation-" + info["fingerprint"][:20]:
            raise ValueError("The original legacy import is missing; restore it before moving this evaluation")
    info = cache_run(info, data_root)
    prepared = []
    for student in info["students"]:
        with fitz.open(student["pdf"]) as document:
            count = len(document)
        if not count:
            raise ValueError("A source PDF has no pages")
        result = student["result"]
        drawings = result.get("drawings", [])
        if not isinstance(drawings, list):
            raise ValueError("Invalid drawing result list")
        for drawing in drawings:
            page = drawing["page_number"]
            if type(page) is not int or not 1 <= page <= count:
                raise ValueError("A finding references an invalid PDF page")
            for finding in drawing.get("mistakes", []):
                for field in ("mistake_id", "mistake_name", "explanation"):
                    if not isinstance(finding.get(field), str):
                        raise ValueError(f"A finding is missing {field}")
        prepared.append((student, count))
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT 1 FROM assignments WHERE id=?", (info["id"],)).fetchone()
        if existing:
            # Preserve accounts, finding IDs, requests, decisions, and their versions.
            db.execute("UPDATE assignments SET bundle=? WHERE id=?", (str(info["root"]), info["id"]))
            for student, _ in prepared:
                db.execute("UPDATE submissions SET pdf=? WHERE id=? AND student_id=?",
                           (str(student["pdf"]), info["id"] + ":" + student["identifier"], student["identifier"]))
            return 0
        db.execute("INSERT INTO assignments VALUES (?,?,?,?)", (info["id"], info["label"], str(info["root"]), time.time()))
        for student, count in prepared:
            identifier, result = student["identifier"], student["result"]
            account = db.execute("SELECT role FROM users WHERE id=?", (identifier,)).fetchone()
            if account and account["role"] != "student":
                raise ValueError("A student identifier conflicts with an instructor account")
            create_user(db, identifier, "student")
            sid = info["id"] + ":" + identifier
            db.execute("INSERT INTO submissions VALUES (?,?,?,?,?,?)",
                       (sid, info["id"], identifier, str(student["pdf"]), count, result.get("notes")))
            for drawing_index, drawing in enumerate(result.get("drawings", [])):
                for index, finding in enumerate(drawing.get("mistakes", [])):
                    db.execute("""INSERT INTO findings
                      (id,submission_id,page,kind,catalog_id,title,description,confidence,boxes,created,updated)
                      VALUES (?,?,?,'ai',?,?,?,?,?,?,?)""",
                      (f"{sid}:{drawing_index}:{index}", sid, drawing["page_number"], finding["mistake_id"],
                       finding["mistake_name"], finding["explanation"], finding.get("confidence", ""),
                       json.dumps(finding.get("bounding_boxes", [])), time.time(), time.time()))
    return len(prepared)


def import_available(roots, data_root, connect, create_user):
    report = {"imported": 0, "reused": 0, "errors": []}
    for folder in discover_runs(roots):
        try:
            count = import_run(folder, data_root, connect, create_user)
            report["imported"] += count
            report["reused"] += count == 0
        except (OSError, ValueError, KeyError, TypeError, fitz.FileDataError) as error:
            report["errors"].append({"folder": folder.name, "error": str(error)})
    return report


def receive_archive(stream, length, imports_root, filename, maximum=250 * 1024 * 1024):
    """Receive one run ZIP; reject traversal, symlinks, duplicate paths, and bombs."""
    if type(length) is not int or not 0 < length <= maximum:
        raise ValueError("Choose an evaluation ZIP no larger than 250 MB")
    imports_root = Path(imports_root).resolve()
    imports_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".upload-", dir=imports_root) as temporary:
        temporary = Path(temporary)
        archive_path = temporary / "upload.zip"
        remaining = length
        with archive_path.open("wb") as output:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("The ZIP upload ended before it was complete")
                output.write(chunk)
                remaining -= len(chunk)
        unpacked = temporary / "unpacked"
        unpacked.mkdir()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries = archive.infolist()
                if len(entries) > 20000 or sum(entry.file_size for entry in entries) > 1024 * 1024 * 1024:
                    raise ValueError("The evaluation ZIP is too large after extraction")
                seen = set()
                for entry in entries:
                    path = PurePosixPath(entry.filename)
                    parts = path.parts
                    if (path.is_absolute() or not parts or "\\" in entry.filename or "\x00" in entry.filename
                            or any(part in ("..", ".") or ":" in part or part.endswith((".", " ")) for part in parts)
                            or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
                        raise ValueError("The ZIP contains an unsafe path, link, or encrypted entry")
                    normalized = path.as_posix().casefold().rstrip("/")
                    if normalized in seen:
                        raise ValueError("The ZIP contains duplicate filenames")
                    seen.add(normalized)
                    target = unpacked.joinpath(*parts)
                    if not target.resolve().is_relative_to(unpacked.resolve()):
                        raise ValueError("The ZIP contains an unsafe path")
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(entry) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
        except (zipfile.BadZipFile, RuntimeError) as error:
            raise ValueError("Choose a valid, unencrypted evaluation ZIP") from error
        if (unpacked / "results.json").is_file():
            source = unpacked
            name = filename[:-4] if filename.lower().endswith(".zip") else filename
        else:
            folders = [path for path in unpacked.iterdir() if path.is_dir() and (path / "results.json").is_file()]
            if len(folders) != 1:
                raise ValueError("The ZIP must contain exactly one completed evaluation folder")
            source, name = folders[0], folders[0].name
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,140}", name):
            raise ValueError("Keep the original evaluation_<date_time> folder name")
        incoming = inspect_run(source)
        run_files(source)
        destination = imports_root / name
        if destination.exists():
            if inspect_run(destination)["fingerprint"] != incoming["fingerprint"]:
                raise ValueError("A different evaluation already has this folder name; upload a new run folder")
            return destination
        source.rename(destination)
        return destination
