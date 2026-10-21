"""Portable evaluation-folder contract shared with the standalone website.

Required: results.json, input/<original PDFs>, reference/mistakes_catalog.csv.
The website carries an identical copy of this stdlib-only reader.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
from uuid import uuid4
import zipfile


def new_run_directory(root: Path) -> Path:
    """Return a recognizable, collision-resistant run path without creating it."""
    return root / (datetime.now().strftime("evaluation_%Y_%m_%d_%H_%M_%S") + "_" + uuid4().hex[:6])


def student_identifier(filename: str) -> str:
    identifier = Path(filename).stem.split("_", 1)[0].lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,79}", identifier):
        raise ValueError(f"Invalid student identifier in {filename!r}")
    return identifier


def safe_filename(name: str) -> str:
    if (not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name
            or not name.lower().endswith(".pdf")):
        raise ValueError("Unsafe source PDF filename")
    return name


def safe_file(root: Path, relative: str) -> Path:
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Missing or unsafe run file: {relative}")
    return path


def snapshot_inputs(run_dir: Path, input_folder: Path, catalog_path: Path) -> tuple[Path, Path]:
    """Freeze original inputs and catalog so a completed run can travel alone."""
    pdfs = sorted(path for path in input_folder.iterdir()
                  if path.is_file() and path.suffix.lower() == ".pdf")
    if not pdfs:
        raise ValueError("No student PDFs found")
    seen = set()
    for pdf in pdfs:
        identifier = student_identifier(safe_filename(pdf.name))
        if identifier in seen:
            raise ValueError(f"Multiple PDFs map to {identifier}; keep one submission per student per run")
        seen.add(identifier)
        if pdf.is_symlink():
            raise ValueError("Student PDFs must not be symbolic links")
        with pdf.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError(f"Not a PDF: {pdf.name}")
    input_copy = run_dir / "input"
    input_copy.mkdir(parents=True, exist_ok=True)
    for pdf in pdfs:
        destination = input_copy / pdf.name
        if destination.resolve() == pdf.resolve():
            continue
        if destination.exists() and destination.read_bytes() != pdf.read_bytes():
            raise ValueError(f"Original input snapshot differs: {pdf.name}")
        if not destination.exists():
            shutil.copyfile(pdf, destination)
    catalog_copy = run_dir / "reference/mistakes_catalog.csv"
    catalog_copy.parent.mkdir(parents=True, exist_ok=True)
    if catalog_copy.resolve() != catalog_path.resolve():
        if catalog_copy.exists() and catalog_copy.read_bytes() != catalog_path.read_bytes():
            raise ValueError("The saved catalog snapshot differs from the supplied catalog")
        if not catalog_copy.exists():
            shutil.copyfile(catalog_path, catalog_copy)
    return input_copy, catalog_copy


def inspect_run(run_dir: Path, *, check_manifest: bool = True) -> dict:
    """Read unchanged portable runs and the earlier per-student import format."""
    run_dir = run_dir.resolve()
    legacy = (run_dir / "manifest.json").is_file() and not (run_dir / "input").is_dir()
    manifest = None
    if legacy:
        manifest = json.loads(safe_file(run_dir, "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1 or not re.fullmatch(r"evaluation-[a-f0-9]{20}", manifest.get("id", "")):
            raise ValueError("Unsupported legacy import manifest")
        catalog = safe_file(run_dir, "mistakes_catalog.csv")
        documents = []
        pdfs = []
        for student in manifest["students"]:
            identifier = student["identifier"]
            name = safe_filename(student["source_pdf"])
            if identifier != student_identifier(name):
                raise ValueError("Student identifier does not match its source PDF")
            document = json.loads(safe_file(run_dir, f"students/{identifier}/results.json").read_text(encoding="utf-8"))
            pdf = safe_file(run_dir, f"students/{identifier}/{name}")
            if hashlib.sha256(pdf.read_bytes()).hexdigest() != student["pdf_sha256"]:
                raise ValueError("PDF hash mismatch")
            if document.get("source_pdf", name) != name:
                raise ValueError("PDF and result filenames do not match")
            document = {**document, "source_pdf": name}
            documents.append(document)
            pdfs.append(pdf)
        results_bytes = ((run_dir / "results.json").read_bytes() if (run_dir / "results.json").is_file()
                         else json.dumps(documents, sort_keys=True).encode())
    else:
        result_path = safe_file(run_dir, "results.json")
        results_bytes = result_path.read_bytes()
        documents = json.loads(results_bytes.decode("utf-8"))
        catalog = safe_file(run_dir, "reference/mistakes_catalog.csv")
        if not isinstance(documents, list) or not documents:
            raise ValueError("Expected a non-empty evaluation result list")
        pdfs = [safe_file(run_dir, "input/" + safe_filename(doc["source_pdf"])) for doc in documents]
        all_inputs = {path.name for path in (run_dir / "input").iterdir()
                      if path.is_file() and path.suffix.lower() == ".pdf"}
        if all_inputs != {doc["source_pdf"] for doc in documents}:
            raise ValueError("The run is incomplete: input PDFs and evaluated documents do not match")
    if not documents:
        raise ValueError("No evaluated students in this run")
    digest = hashlib.sha256(results_bytes + catalog.read_bytes())
    students, seen = [], set()
    for document, pdf in zip(documents, pdfs):
        identifier = student_identifier(document["source_pdf"])
        if identifier in seen:
            raise ValueError(f"Multiple PDFs map to {identifier}; keep one submission per student per run")
        seen.add(identifier)
        with pdf.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError(f"Not a PDF: {pdf.name}")
        pdf_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
        digest.update(pdf_hash.encode())
        students.append({"identifier": identifier, "pdf": pdf, "result": document, "pdf_sha256": pdf_hash})
    fingerprint = digest.hexdigest()
    identity = manifest["id"] if legacy else "evaluation-" + fingerprint[:20]
    ready_path = run_dir / "run_manifest.json"
    if check_manifest and ready_path.is_file():
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        if ready.get("format") != "plana.evaluation.v1" or ready.get("fingerprint") != fingerprint:
            raise ValueError("Run files changed after finalization; create a new evaluation run")
    return {"id": identity, "fingerprint": fingerprint, "label": manifest["label"] if legacy else run_dir.name,
            "root": run_dir, "catalog": catalog, "students": students, "legacy": legacy}


def finalize_run(run_dir: Path) -> dict:
    """Write a completion marker without changing result/PDF/catalog contents."""
    data = inspect_run(run_dir, check_manifest=False)
    existing = run_dir / "run_manifest.json"
    if existing.is_file():
        previous = json.loads(existing.read_text(encoding="utf-8"))
        if previous.get("fingerprint") == data["fingerprint"] and previous.get("format") == "plana.evaluation.v1":
            return data
    marker = {"format": "plana.evaluation.v1", "fingerprint": data["fingerprint"],
              "student_count": len(data["students"]), "completed_at": datetime.now().astimezone().isoformat()}
    (run_dir / "run_manifest.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
    return data


def run_files(run_dir: Path) -> list[Path]:
    """Do not follow links or package environment files into handoff archives."""
    files = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_symlink():
            raise ValueError("Evaluation folders must not contain symbolic links")
        if path.name in {".env", ".git"} or path.suffix.lower() in {".pem", ".key"}:
            raise ValueError("Remove secrets or Git metadata from the evaluation folder")
        if path.is_file():
            files.append(path)
    return files


def archive_run(run_dir: Path) -> Path:
    """Zip the whole original folder, preserving its paths and folder name."""
    inspect_run(run_dir)
    files = run_files(run_dir)
    archives = run_dir.parent / "_archives"
    archives.mkdir(parents=True, exist_ok=True)
    destination = archives / (run_dir.name + ".zip")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=(Path(run_dir.name) / path.relative_to(run_dir)).as_posix())
    return destination
