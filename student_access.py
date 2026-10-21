"""Instructor roster review, credential generation, and tracked invitations."""
from __future__ import annotations

import csv
import hmac
import io
import os
from pathlib import Path
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlparse


class AccessError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def initialize(db, data_root, hash_key):
    """Migrate existing accounts without replacing their credentials."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    additions = {"display_name": "TEXT DEFAULT ''", "roster_version": "INTEGER DEFAULT 0",
                 "access_key": "TEXT", "key_version": "INTEGER DEFAULT 1",
                 "key_email": "TEXT", "last_sent_email": "TEXT"}
    for name, declaration in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE users ADD COLUMN {name} {declaration}")
    db.execute("""CREATE TABLE IF NOT EXISTS invitations (
      user_id TEXT REFERENCES users(id), roster_version INTEGER, key_version INTEGER,
      recipient TEXT, name TEXT, status TEXT, error TEXT DEFAULT '', actor TEXT,
      created REAL, sent_at REAL, PRIMARY KEY(user_id,roster_version,key_version))""")
    credentials = Path(data_root) / "private/login_keys.csv"
    if credentials.is_file():
        with credentials.open(encoding="utf-8", newline="") as stream:
            saved = {row["identifier"]: row["key"] for row in csv.DictReader(stream)}
        for user in db.execute("SELECT * FROM users WHERE role='student' AND access_key IS NULL").fetchall():
            key = saved.get(user["id"])
            if key and hmac.compare_digest(hash_key(key, user["salt"]), user["hash"]):
                db.execute("UPDATE users SET access_key=? WHERE id=?", (key, user["id"]))
    db.execute("UPDATE users SET last_sent_email=email WHERE invited_at IS NOT NULL AND last_sent_email IS NULL")


def mail_configuration():
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "PLANA_PUBLIC_URL"]
    missing = [name for name in required if not os.environ.get(name)]
    url = os.environ.get("PLANA_PUBLIC_URL", "").rstrip("/")
    parsed = urlparse(url)
    if url and (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password):
        missing.append("PLANA_PUBLIC_URL must be an HTTPS website URL")
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        missing.append("SMTP_PORT must be a valid port")
    return {"ready": not missing, "missing": missing, "public_url": url,
            "sender": os.environ.get("SMTP_FROM", "")}


def require_instructor(user):
    if user["role"] != "instructor":
        raise AccessError(403, "Only instructors can manage student access.")


def student_rows(db):
    return db.execute("""SELECT u.* FROM users u WHERE role='student'
      AND EXISTS(SELECT 1 FROM submissions s WHERE s.student_id=u.id) ORDER BY u.id""").fetchall()


def needs_key(user):
    return not user["access_key"] or bool(user["last_sent_email"] and
        user["last_sent_email"] != user["email"] and user["key_email"] != user["email"])


def overview(db, user):
    require_instructor(user)
    students = []
    for row in student_rows(db):
        latest = db.execute("""SELECT status,error,sent_at FROM invitations WHERE user_id=?
          AND roster_version=? AND key_version=?""", (row["id"], row["roster_version"], row["key_version"])).fetchone()
        status = latest["status"] if latest else ("sent" if row["invited_at"] and row["last_sent_email"] == row["email"] else "not_sent")
        students.append({"identifier": row["id"], "name": row["display_name"] or "", "email": row["email"] or "",
            "version": row["roster_version"], "has_key": not needs_key(row), "status": status,
            "sent_at": latest["sent_at"] if latest else row["invited_at"], "error": latest["error"] if latest else "",
            "pdfs": sorted({Path(item[0]).name for item in db.execute("SELECT pdf FROM submissions WHERE student_id=?", (row["id"],))})})
    return {"students": students, "mail": mail_configuration()}


def clean_contact(row):
    if not isinstance(row, dict):
        raise AccessError(400, "Each roster entry must contain identifier, name, and email.")
    identifier, name, email = (row.get(key, "") for key in ("identifier", "name", "email"))
    if not all(isinstance(value, str) for value in (identifier, name, email)):
        raise AccessError(400, "Identifiers, names, and emails must be text.")
    identifier, name, email = identifier.strip().lower(), name.strip(), email.strip().lower()
    import re
    if not identifier or not name or len(name) > 160 or any(ord(char) < 32 for char in name):
        raise AccessError(400, f"Enter the roster name for {identifier or 'each student'}.")
    if not re.fullmatch(r"[a-z0-9._%+-]+@wisc\.edu", email) or len(email) > 254:
        raise AccessError(400, f"Enter a valid wisc.edu email for {identifier}.")
    return {"identifier": identifier, "name": name, "email": email}


def preview_roster(db, user, csv_text):
    require_instructor(user)
    if not isinstance(csv_text, str) or len(csv_text) > 1_000_000:
        raise AccessError(400, "Choose a roster CSV smaller than 1 MB.")
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    if not reader.fieldnames or not {"identifier", "name", "email"}.issubset(reader.fieldnames):
        raise AccessError(400, "The CSV needs identifier,name,email column headers.")
    known = {row["id"]: row for row in student_rows(db)}
    rows, errors, identifiers, emails = [], [], set(), set()
    for line, row in enumerate(reader, start=2):
        if not any(row.values()):
            continue
        try:
            cleaned = clean_contact(row)
            identifier, email = cleaned["identifier"], cleaned["email"]
            if identifier not in known:
                raise AccessError(400, f"No imported PDF matches identifier {identifier}.")
            if identifier in identifiers or email in emails:
                raise AccessError(400, "Duplicate identifier or email in the roster.")
            identifiers.add(identifier)
            emails.add(email)
            cleaned["version"] = known[identifier]["roster_version"]
            rows.append(cleaned)
        except AccessError as error:
            errors.append(f"Row {line}: {error.message}")
    if not rows and not errors:
        errors.append("The roster contains no student rows.")
    return {"rows": rows, "errors": errors}


def save_roster(db, user, rows):
    require_instructor(user)
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        raise AccessError(400, "Choose 1–1000 roster rows to save.")
    db.execute("BEGIN IMMEDIATE")
    known = {row["id"]: row for row in student_rows(db)}
    prepared, seen = [], set()
    for row in rows:
        cleaned = clean_contact(row)
        identifier = cleaned["identifier"]
        if identifier not in known or identifier in seen:
            raise AccessError(400, "Each roster row must match a different imported student.")
        if type(row.get("version")) is not int or row["version"] != known[identifier]["roster_version"]:
            raise AccessError(409, "Another instructor updated this roster. Reload student access.")
        if db.execute("SELECT 1 FROM invitations WHERE user_id=? AND status='sending'", (identifier,)).fetchone():
            raise AccessError(409, "An invitation is being sent. Wait before changing that recipient.")
        seen.add(identifier)
        prepared.append(cleaned)
    effective = {key: (value["email"] or "").lower() for key, value in known.items()}
    effective.update({row["identifier"]: row["email"] for row in prepared})
    emails = [email for email in effective.values() if email]
    if len(emails) != len(set(emails)):
        raise AccessError(400, "Two students have the same email. Check the roster before saving.")
    for row in prepared:
        old = known[row["identifier"]]
        if row["name"] != old["display_name"] or row["email"] != old["email"]:
            db.execute("UPDATE users SET display_name=?,email=?,roster_version=roster_version+1 WHERE id=?",
                       (row["name"], row["email"], row["identifier"]))
    return {"saved": len(prepared)}


def generate_keys(db, user, data_root, hash_key):
    """Keep existing keys, except when a previously emailed recipient changes."""
    require_instructor(user)
    db.execute("BEGIN IMMEDIATE")
    changed = 0
    for student in student_rows(db):
        if not needs_key(student):
            continue
        if db.execute("SELECT 1 FROM invitations WHERE user_id=? AND status='sending'", (student["id"],)).fetchone():
            raise AccessError(409, "Wait for invitation delivery before generating keys.")
        key, salt = secrets.token_urlsafe(24), secrets.token_hex(16)
        db.execute("UPDATE users SET access_key=?,salt=?,hash=?,key_version=key_version+1,key_email=? WHERE id=?",
                   (key, salt, hash_key(key, salt), student["email"], student["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=?", (student["id"],))
        path = Path(data_root) / "private/login_keys.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            if new:
                writer.writerow(["identifier", "role", "key"])
            writer.writerow([student["id"], "student", key])
        changed += 1
    keys = {row["id"]: row["access_key"] for row in student_rows(db)}
    return {"keys": keys, "generated": changed}


def invitation_message(sender, url, student):
    message = EmailMessage()
    message["From"], message["To"] = sender, student["email"]
    message["Subject"] = "Your Plana drawing review access"
    message.set_content(f"Hello {student['display_name']},\n\nYour drawing review is ready.\n\nOpen: {url}\nStudent identifier: {student['id']}\nAccess key: {student['access_key']}\n\nKeep your access key private. You can review AI findings, submit rebuttals, and report unidentified mistakes.")
    return message


def send_invitations(connect, user, recipients):
    """Claim reviewed snapshots before delivery so two instructors cannot double-send."""
    require_instructor(user)
    configuration = mail_configuration()
    if not configuration["ready"]:
        raise AccessError(400, "Email is not configured: " + ", ".join(configuration["missing"]))
    if not isinstance(recipients, list) or not recipients or len(recipients) > 500:
        raise AccessError(400, "Select 1–500 reviewed students.")
    jobs, skipped, seen = [], [], set()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for recipient in recipients:
            contact = clean_contact(recipient)
            identifier = contact["identifier"]
            if identifier in seen or recipient.get("reviewed") is not True:
                raise AccessError(400, "Confirm each student's name, email, and PDF identifier before sending.")
            seen.add(identifier)
            student = db.execute("SELECT * FROM users WHERE id=? AND role='student' AND EXISTS(SELECT 1 FROM submissions WHERE student_id=users.id)", (identifier,)).fetchone()
            if not student or type(recipient.get("version")) is not int or recipient["version"] != student["roster_version"] or contact["name"] != student["display_name"] or contact["email"] != student["email"]:
                raise AccessError(409, "The recipient details changed. Save, reload, and check the roster again.")
            if needs_key(student):
                raise AccessError(409, "Generate login keys for the current recipients before sending.")
            existing = db.execute("SELECT status FROM invitations WHERE user_id=? AND roster_version=? AND key_version=?", (identifier, student["roster_version"], student["key_version"])).fetchone()
            if (existing and existing["status"] == "sent") or (not existing and student["invited_at"] and student["last_sent_email"] == student["email"]):
                skipped.append(identifier)
                continue
            if existing and existing["status"] == "sending":
                raise AccessError(409, "An instructor is already sending this invitation.")
            jobs.append(dict(student))
        for student in jobs:
            db.execute("""INSERT INTO invitations(user_id,roster_version,key_version,recipient,name,status,actor,created)
              VALUES (?,?,?,?,?,'sending',?,?) ON CONFLICT(user_id,roster_version,key_version)
              DO UPDATE SET status='sending',error='',actor=excluded.actor,created=excluded.created""",
              (student["id"], student["roster_version"], student["key_version"], student["email"], student["display_name"], user["id"], time.time()))
    sent, failed = [], []
    def record(student, success):
        now = time.time()
        with connect() as db:
            db.execute("UPDATE invitations SET status=?,error=?,sent_at=? WHERE user_id=? AND roster_version=? AND key_version=?",
                       ("sent" if success else "failed", "" if success else "Delivery failed. Check the mail settings and recipient address, then retry.",
                        now if success else None, student["id"], student["roster_version"], student["key_version"]))
            if success:
                db.execute("UPDATE users SET invited_at=?,last_sent_email=?,key_email=? WHERE id=?",
                           (now, student["email"], student["email"], student["id"]))
        (sent if success else failed).append(student["id"])
    if jobs:
        remaining = list(jobs)
        try:
            with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=20) as mail:
                mail.starttls(context=ssl.create_default_context())
                mail.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                for student in jobs:
                    try:
                        refused = mail.send_message(invitation_message(configuration["sender"], configuration["public_url"], student))
                        record(student, not bool(refused))
                    except (smtplib.SMTPException, OSError):
                        record(student, False)
                    remaining.pop(0)
        except (smtplib.SMTPException, OSError):
            for student in remaining:
                record(student, False)
    return {"sent": sent, "failed": failed, "skipped": skipped}


def credentials(db, user):
    """Return student credentials only to authenticated instructors."""
    require_instructor(user)
    return {"students": [{"identifier": row["id"], "name": row["display_name"] or "",
        "email": row["email"] or "", "key": row["access_key"] or "", "version": row["roster_version"],
        "pdfs": sorted({Path(item[0]).name for item in db.execute("SELECT pdf FROM submissions WHERE student_id=?", (row["id"],))})}
        for row in student_rows(db)]}


def credentials_csv(db, user):
    """Build an exact credential export, including students missing roster details."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["identifier", "name", "email", "key"])
    for student in credentials(db, user)["students"]:
        writer.writerow([student[key] for key in ("identifier", "name", "email", "key")])
    return stream.getvalue().encode("utf-8-sig")


def write_credentials_export(db, data_root):
    """Keep a private local CSV ready for the instructor's manual email workflow."""
    import tempfile
    folder = Path(data_root) / "private"
    folder.mkdir(parents=True, exist_ok=True)
    content = credentials_csv(db, {"role": "instructor"})
    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
        stream.write(content)
        temporary = Path(stream.name)
    try:
        temporary.chmod(0o600)
        os.replace(temporary, folder / "student_credentials.csv")
    finally:
        temporary.unlink(missing_ok=True)
