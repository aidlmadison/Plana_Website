"""Plana: authenticated drawing review with durable student/instructor workflows."""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import math
import os
from pathlib import Path
import re
import secrets
import smtplib
import sqlite3
import student_access
import run_imports
import time
from email.message import EmailMessage
from urllib.parse import urlparse, parse_qs, unquote

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("PLANA_DATA_DIR", ROOT / "data")).resolve()
IMPORTS = Path(os.environ.get("PLANA_IMPORTS_DIR", ROOT / "imports")).resolve()
STATIC = ROOT / "static"
SESSION_SECONDS = 12 * 3600


class Problem(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


class Database(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / "plana.sqlite3", timeout=30, factory=Database)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def password_hash(key: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", key.encode(), salt.encode(), 300_000).hex()


def init_db(instructors: int = 2) -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, role TEXT NOT NULL, salt TEXT NOT NULL, hash TEXT NOT NULL,
          email TEXT, invited_at REAL);
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), csrf TEXT, expires REAL);
        CREATE TABLE IF NOT EXISTS attempts (address TEXT, at REAL);
        CREATE TABLE IF NOT EXISTS assignments (
          id TEXT PRIMARY KEY, label TEXT, bundle TEXT, imported REAL);
        CREATE TABLE IF NOT EXISTS submissions (
          id TEXT PRIMARY KEY, assignment_id TEXT REFERENCES assignments(id),
          student_id TEXT REFERENCES users(id), pdf TEXT, pages INTEGER, notes TEXT);
        CREATE TABLE IF NOT EXISTS findings (
          id TEXT PRIMARY KEY, submission_id TEXT REFERENCES submissions(id),
          page INTEGER, kind TEXT, catalog_id TEXT, title TEXT, description TEXT,
          confidence TEXT, boxes TEXT, status TEXT DEFAULT 'open', argument TEXT DEFAULT '',
          feedback TEXT DEFAULT '', reviewer TEXT, version INTEGER DEFAULT 0,
          created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, actor TEXT, finding TEXT, action TEXT, payload TEXT, at REAL);
        """)
        student_access.initialize(db, DATA, password_hash)
        for index in range(1, instructors + 1):
            create_user(db, f"instructor{index}", "instructor")


def create_user(db, identifier: str, role: str) -> None:
    if db.execute("SELECT 1 FROM users WHERE id=?", (identifier,)).fetchone():
        return
    key, salt = secrets.token_urlsafe(24), secrets.token_hex(16)
    db.execute("INSERT INTO users(id,role,salt,hash) VALUES (?,?,?,?)",
               (identifier, role, salt, password_hash(key, salt)))
    if role == 'student':
        db.execute('UPDATE users SET access_key=? WHERE id=?', (key, identifier))
    private = DATA / "private"
    private.mkdir(exist_ok=True)
    credentials = private / "login_keys.csv"
    new = not credentials.exists()
    with credentials.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        if new:
            writer.writerow(["identifier", "role", "key"])
        writer.writerow([identifier, role, key])
    try:
        credentials.chmod(0o600)
    except OSError:
        pass


def safe_file(base: Path, relative: str) -> Path:
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ValueError("Invalid import file")
    return path


def refresh_imports() -> dict:
    """Scan portable run folders and earlier handoffs without resetting reviews."""
    return run_imports.import_available([IMPORTS, DATA / "imports"], DATA, connect, create_user)


def import_latest() -> int:
    return refresh_imports()["imported"]


def grading() -> dict:
    defaults = {"maximum": 60, "base_deduction": 1, "repeat_decay": 0.5,
                "self_report_credit": 1}
    path = ROOT / "grading.json"
    if path.exists():
        defaults.update(json.loads(path.read_text(encoding="utf-8")))
    for key, value in defaults.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid grading value: {key}")
    if defaults["repeat_decay"] > 1:
        raise ValueError("Repeat decay must be between 0 and 1")
    return defaults


def calculate_scores(findings: list[dict]) -> dict:
    config = grading()
    def deductions(include_accepted):
        seen, total = {}, 0.0
        for finding in findings:
            if finding["kind"] != "ai" or (not include_accepted and finding["status"] == "accepted"):
                continue
            key = finding["catalog_id"]
            count = seen.get(key, 0)
            total += config["base_deduction"] * config["repeat_decay"] ** count
            seen[key] = count + 1
        return total
    seen, credit = {}, 0.0
    for finding in findings:
        if finding["kind"] == "self" and finding["status"] == "accepted":
            key = finding["catalog_id"] or "SELF"
            count = seen.get(key, 0)
            credit += config["self_report_credit"] * config["repeat_decay"] ** count
            seen[key] = count + 1
    maximum = config["maximum"]
    return {"original": round(max(0, maximum - deductions(True)), 4),
            "adjusted": round(min(maximum, max(0, maximum - deductions(False) + credit)), 4),
            "credit": round(credit, 4), "maximum": maximum, "rules": config}


def text_value(data: dict, key: str, maximum: int = 6000) -> str:
    value = data.get(key, "")
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Problem(400, f"Enter {key} (1–{maximum} characters).")
    return value.strip()


def validate_box(value) -> list:
    if not isinstance(value, list) or len(value) != 4:
        raise Problem(400, "Draw exactly one bounding box.")
    if not all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1000 for n in value):
        raise Problem(400, "Invalid box coordinates.")
    if value[0] >= value[2] or value[1] >= value[3]:
        raise Problem(400, "Draw a box with positive width and height.")
    return value


def authorize_submission(db, user, sid):
    submission = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not submission or (user["role"] == "student" and submission["student_id"] != user["id"]):
        raise Problem(404, "Submission not found.")
    return submission


def change_finding(db, user, data):
    """One transaction serializes edits from both instructors and checks versions."""
    db.execute("BEGIN IMMEDIATE")
    action = data.get("action")
    now = time.time()
    if action == "self_report":
        if user["role"] != "student":
            raise Problem(403, "Only students can report a mistake.")
        submission = authorize_submission(db, user, data.get("submission"))
        page = data.get("page")
        if type(page) is not int or not 1 <= page <= submission["pages"]:
            raise Problem(400, "Invalid page.")
        description = text_value(data, "description")
        boxes = [{"coordinate_space": "page_normalized_1000", "box_2d": validate_box(data.get("box")),
                  "zone": "Student reported region"}]
        catalog_id = data.get("catalog_id", "")
        if catalog_id and catalog_id not in {item["id"] for item in read_catalog(db, submission["assignment_id"])}:
            raise Problem(400, "Choose a catalog mistake or leave it unclassified.")
        fid = secrets.token_hex(16)
        db.execute("""INSERT INTO findings (id,submission_id,page,kind,catalog_id,title,description,
                    confidence,boxes,status,argument,created,updated)
                    VALUES (?,?,?,'self',?,'Self-reported mistake',?,'',?,'pending',?,?,?)""",
                   (fid, submission["id"], page, catalog_id, description, json.dumps(boxes), description, now, now))
    else:
        fid = data.get("finding")
        finding = db.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
        if not finding:
            raise Problem(404, "Finding not found.")
        authorize_submission(db, user, finding["submission_id"])
        if type(data.get("version")) is not int or finding["version"] != data["version"]:
            raise Problem(409, "This item changed. Refresh and review the latest version.")
        if action == "rebuttal":
            if user["role"] != "student" or finding["kind"] != "ai" or finding["status"] != "open":
                raise Problem(409, "This finding is not available for rebuttal.")
            db.execute("UPDATE findings SET status='pending',argument=? WHERE id=?",
                       (text_value(data, "argument"), fid))
        elif action == "cancel":
            if user["role"] != "student" or finding["status"] != "pending":
                raise Problem(409, "Only a pending request can be canceled.")
            status = "open" if finding["kind"] == "ai" else "canceled"
            db.execute("UPDATE findings SET status=?,argument='',feedback='',reviewer=NULL WHERE id=?", (status, fid))
        elif action == "edit_box":
            if user["role"] != "student" or finding["kind"] != "self" or finding["status"] != "pending":
                raise Problem(409, "Only a pending self-report can be revised.")
            boxes = [{"coordinate_space": "page_normalized_1000", "box_2d": validate_box(data.get("box")),
                      "zone": "Student reported region"}]
            db.execute("UPDATE findings SET boxes=? WHERE id=?", (json.dumps(boxes), fid))
        elif action == "review":
            if user["role"] != "instructor" or finding["status"] not in ("pending", "accepted", "rejected"):
                raise Problem(409, "Only instructors can decide submitted student requests.")
            if type(data.get("agree")) is not bool:
                raise Problem(400, "Choose agree or disagree.")
            db.execute("UPDATE findings SET status=?,feedback=?,reviewer=? WHERE id=?",
                       ("accepted" if data["agree"] else "rejected", text_value(data, "feedback"), user["id"], fid))
        else:
            raise Problem(400, "Unknown action.")
        db.execute("UPDATE findings SET version=version+1,updated=? WHERE id=?", (now, fid))
    db.execute("INSERT INTO audit(actor,finding,action,payload,at) VALUES (?,?,?,?,?)",
               (user["id"], fid, action, json.dumps(data), now))
    return {"id": fid}


def read_catalog(db, assignment_id):
    row = db.execute("SELECT bundle FROM assignments WHERE id=?", (assignment_id,)).fetchone()
    if not row:
        return []
    folder = Path(row["bundle"])
    catalog = folder / "reference/mistakes_catalog.csv"
    raw = (catalog if catalog.is_file() else folder / "mistakes_catalog.csv").read_bytes()
    text = raw.decode("utf-8-sig", errors="surrogateescape")
    text = "".join(bytes([ord(char) - 0xDC00]).decode("cp1252")
                   if 0xDC80 <= ord(char) <= 0xDCFF else char for char in text)
    with io.StringIO(text, newline="") as stream:
        return [{"id": f"M{int(item['ID']):02d}", "description": item["Cleaned Mistake Description"],
                 "theme": item.get("Theme", ""), "example": item.get("One Example", "")}
                for item in csv.DictReader(stream)]


class Handler(BaseHTTPRequestHandler):
    server_version = "Plana"

    def log_message(self, fmt, *args):
        # Query strings and request bodies can contain private information.
        pass

    def respond(self, status, body, content_type="application/json", cookie=None, download=None):
        if content_type == "application/json":
            body = json.dumps(body, allow_nan=False).encode()
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def current_user(self, db, csrf=False):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie["plana_session"].value
        except (KeyError, ValueError):
            raise Problem(401, "Please sign in.")
        session = db.execute("""SELECT users.*,sessions.csrf,sessions.expires FROM sessions
          JOIN users ON users.id=sessions.user_id WHERE token=?""",
          (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not session or session["expires"] <= time.time():
            raise Problem(401, "Your session expired. Please sign in.")
        if csrf and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
            raise Problem(403, "Please refresh before submitting.")
        return session

    def do_GET(self):
        try:
            route = urlparse(self.path)
            query = parse_qs(route.query)
            if route.path in ("/", "/app.js", "/style.css"):
                filename, content_type = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}[route.path]
                return self.respond(200, (STATIC / filename).read_bytes(), content_type)
            with connect() as db:
                user = self.current_user(db)
                if route.path == "/api/me":
                    return self.respond(200, {"id": user["id"], "role": user["role"], "csrf": user["csrf"]})
                if route.path == "/api/credentials":
                    return self.respond(200, student_access.credentials(db, user))
                if route.path == "/api/credentials.csv":
                    content = student_access.credentials_csv(db, user)
                    student_access.write_credentials_export(db, DATA)
                    return self.respond(200, content, "text/csv; charset=utf-8", download="student_credentials.csv")
                if route.path == "/api/access":
                    return self.respond(200, student_access.overview(db, user))
                if route.path == "/api/submissions":
                    sql = """SELECT s.*, a.label, a.imported,
                       (SELECT COUNT(*) FROM findings f WHERE f.submission_id=s.id AND f.status='pending') pending
                       FROM submissions s JOIN assignments a ON s.assignment_id=a.id"""
                    rows = db.execute(sql + (" WHERE s.student_id=?" if user["role"] == "student" else "") +
                                      " ORDER BY a.imported DESC,s.student_id", (user["id"],) if user["role"] == "student" else ()).fetchall()
                    return self.respond(200, [{k: row[k] for k in ("id", "student_id", "assignment_id", "label", "pages", "pending")}
                                             for row in rows])
                sid = query.get("submission", [""])[0]
                submission = authorize_submission(db, user, sid)
                if route.path == "/api/submission":
                    findings = [dict(row) for row in db.execute("SELECT * FROM findings WHERE submission_id=? AND status!='canceled' ORDER BY page,created,id", (sid,))]
                    for finding in findings:
                        finding["boxes"] = json.loads(finding["boxes"])
                    return self.respond(200, {"id": sid, "student_id": submission["student_id"], "pages": submission["pages"],
                       "notes": submission["notes"], "findings": findings, "scores": calculate_scores(findings),
                       "catalog": read_catalog(db, submission["assignment_id"])})
                if route.path in ("/api/page", "/api/pdf"):
                    pdf = Path(submission["pdf"])
                    if route.path == "/api/pdf":
                        return self.respond(200, pdf.read_bytes(), "application/pdf")
                    try:
                        page = int(query.get("page", ["1"])[0])
                        width = min(3200, max(600, int(query.get("width", ["1800"])[0])))
                    except ValueError:
                        raise Problem(400, "Invalid page or width.")
                    if not 1 <= page <= submission["pages"]:
                        raise Problem(400, "Invalid page.")
                    import fitz
                    with fitz.open(pdf) as document:
                        sheet = document[page - 1]
                        scale = min(width / sheet.rect.width, 3200 / sheet.rect.height)
                        pixmap = sheet.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                        return self.respond(200, pixmap.tobytes("png"), "image/png")
            raise Problem(404, "Not found.")
        except (Problem, student_access.AccessError) as error:
            self.respond(error.status, {"error": error.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.respond(500, {"error": "Could not load this page. Check the server configuration."})

    def do_POST(self):
        try:
            origin = self.headers.get("Origin")
            scheme = "https" if os.environ.get("PLANA_HTTPS") == "1" else "http"
            if origin and origin != f"{scheme}://{self.headers.get('Host')}":
                raise Problem(403, "Cross-origin requests are not allowed.")
            route = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            if route == "/api/import-run":
                with connect() as db:
                    user = self.current_user(db, csrf=True)
                    student_access.require_instructor(user)
                try:
                    folder = run_imports.receive_archive(self.rfile, length, IMPORTS,
                        unquote(self.headers.get("X-File-Name", "evaluation.zip")))
                    report = refresh_imports()
                except (OSError, ValueError, KeyError, TypeError) as error:
                    raise Problem(400, str(error))
                with connect() as export_db:
                    student_access.write_credentials_export(export_db, DATA)
                return self.respond(200, {**report, "folder": folder.name})
            limit = 2_000_000 if route == "/api/access/roster-preview" else 32000
            if not 0 < length <= limit:
                raise Problem(400, "Invalid request size.")
            try:
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                raise Problem(400, "Invalid request.")
            route = urlparse(self.path).path
            with connect() as db:
                if route == "/api/login":
                    return self.login(db, data)
                user = self.current_user(db, csrf=True)
                if route == "/api/logout":
                    db.execute("DELETE FROM sessions WHERE user_id=? AND csrf=?", (user["id"], user["csrf"]))
                    return self.respond(200, {}, "application/json", "plana_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
                if route == "/api/access/roster-preview":
                    return_value = student_access.preview_roster(db, user, data.get("csv"))
                elif route == "/api/access/roster-save":
                    return_value = student_access.save_roster(db, user, data.get("rows"))
                elif route == "/api/access/keys":
                    return_value = student_access.generate_keys(db, user, DATA, password_hash)
                elif route == "/api/access/send":
                    student_access.require_instructor(user)
                    raise student_access.AccessError(410, "Email sending was removed. Download student credentials for manual delivery.")
                elif route == "/api/action":
                    return_value = change_finding(db, user, data)
                elif route == "/api/refresh-imports" and user["role"] == "instructor":
                    return_value = refresh_imports()
                else:
                    raise Problem(404, "Not found.")
            if route in ("/api/access/roster-save", "/api/refresh-imports", "/api/access/keys"):
                with connect() as export_db:
                    student_access.write_credentials_export(export_db, DATA)
            self.respond(200, return_value)
        except (Problem, student_access.AccessError) as error:
            self.respond(error.status, {"error": error.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.respond(500, {"error": "Could not save this change. Try refreshing."})

    def login(self, db, data):
        address, now = self.client_address[0], time.time()
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM attempts WHERE at<?", (now - 900,))
        count = db.execute("SELECT COUNT(*) FROM attempts WHERE address=?", (address,)).fetchone()[0]
        if count >= 20:
            raise Problem(429, "Too many attempts. Try again in 15 minutes.")
        db.execute("INSERT INTO attempts VALUES (?,?)", (address, now))
        db.commit()
        identifier = text_value(data, "identifier", 80).lower()
        key = text_value(data, "key", 200)
        user = db.execute("SELECT * FROM users WHERE id=?", (identifier,)).fetchone()
        hashed = password_hash(key, user["salt"] if user else "invalid-user-salt")
        if not user or not hmac.compare_digest(hashed, user["hash"]) or data.get("role") != user["role"]:
            raise Problem(401, "Identifier or access key is incorrect.")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute("DELETE FROM sessions WHERE expires<?", (now,))
        db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), identifier, csrf, now + SESSION_SECONDS))
        db.commit()
        secure = "; Secure" if os.environ.get("PLANA_HTTPS") == "1" else ""
        self.respond(200, {"id": identifier, "role": user["role"], "csrf": csrf}, cookie=
                     f"plana_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}{secure}")


def roster_import(path: Path, send: bool = False):
    """Validate the entire roster before updating addresses or sending invitations."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    seen = set()
    with connect() as db:
        for row in rows:
            identifier = row.get("identifier", "").strip().lower()
            email = row.get("email", "").strip().lower()
            if not re.fullmatch(r"[a-z0-9._%+-]+@wisc\.edu", email) or identifier in seen:
                raise ValueError("Roster must contain unique identifiers and wisc.edu email addresses")
            seen.add(identifier)
            if not db.execute("SELECT 1 FROM users WHERE id=? AND role='student'", (identifier,)).fetchone():
                raise ValueError(f"No imported student named {identifier}")
            row["identifier"], row["email"] = identifier, email
        for row in rows:
            db.execute("""UPDATE users SET email=?,display_name=CASE WHEN ?!='' THEN ? ELSE display_name END,
                       roster_version=roster_version+CASE WHEN COALESCE(email,'')!=? OR (?!='' AND display_name!=?) THEN 1 ELSE 0 END
                       WHERE id=?""", (row["email"], row.get("name", ""), row.get("name", ""), row["email"],
                       row.get("name", ""), row.get("name", ""), row["identifier"]))
    with connect() as export_db:
        student_access.write_credentials_export(export_db, DATA)
    if not send:
        print(f"Saved {len(rows)} roster addresses; no invitations sent.")
        return
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "PLANA_PUBLIC_URL"]
    if any(not os.environ.get(name) for name in required):
        raise ValueError("Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, and PLANA_PUBLIC_URL")
    url = os.environ["PLANA_PUBLIC_URL"].rstrip("/")
    if not url.startswith("https://"):
        raise ValueError("Invitation URL must use HTTPS")
    with (DATA / "private" / "login_keys.csv").open(encoding="utf-8", newline="") as stream:
        keys = {row["identifier"]: row["key"] for row in csv.DictReader(stream)}
    # Verify all secrets before contacting the mail service.
    if any(row["identifier"] not in keys for row in rows):
        raise ValueError("A student's private login key is missing")
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=30) as mail:
        import ssl
        mail.starttls(context=ssl.create_default_context())
        mail.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        sent = 0
        for row in rows:
            with connect() as db:
                user = db.execute("SELECT * FROM users WHERE id=?", (row["identifier"],)).fetchone()
                if user["invited_at"]:
                    continue
                message = EmailMessage()
                message["From"], message["To"] = os.environ["SMTP_FROM"], row["email"]
                message["Subject"] = "Your Plana drawing review access"
                message.set_content(f"Your drawing review is ready.\n\nOpen: {url}\nStudent identifier: {row['identifier']}\nAccess key: {keys[row['identifier']]}\n\nKeep your access key private. You can review AI findings, submit rebuttals, and report unidentified mistakes.")
                mail.send_message(message)
                db.execute("UPDATE users SET invited_at=?,last_sent_email=?,key_email=? WHERE id=?",
                           (time.time(), row["email"], row["email"], row["identifier"]))
                sent += 1
    print(f"Sent {sent} invitations; previously invited students were skipped.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["serve", "import", "roster"], default="serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--instructors", type=int, default=2)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--send", action="store_true", help="Send roster invitations using configured SMTP")
    args = parser.parse_args()
    if not 1 <= args.instructors <= 100:
        parser.error("Instructor count must be between 1 and 100")
    init_db(args.instructors)
    report = refresh_imports()
    imported = report["imported"]
    for error in report["errors"]:
        print(f"Import skipped: {error['folder']}: {error['error']}", flush=True)
    with connect() as export_db:
        student_access.write_credentials_export(export_db, DATA)
    if args.command == "roster":
        if args.file is None:
            parser.error("roster needs --file roster.csv")
        roster_import(args.file, args.send)
        return
    if args.command == "import":
        print(f"Imported {imported} submissions. Private login keys: data/private/login_keys.csv")
        return
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Plana is ready at http://{args.host}:{args.port}", flush=True)
    print("Private login keys: data/private/login_keys.csv", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
