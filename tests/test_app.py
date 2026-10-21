"""Deterministic workflow, permissions, import, and HTTP tests."""
import csv
import hashlib
import http.client
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import fitz


def make_bundle(root):
    bundle_id = "evaluation-" + "a" * 20
    bundle = root / "imports" / bundle_id
    students = []
    for identifier in ["alice", "bob"]:
        folder = bundle / "students" / identifier
        folder.mkdir(parents=True)
        pdf = folder / (identifier + "_assignment.pdf")
        with fitz.open() as doc:
            doc.new_page(width=400, height=300).insert_text((30, 40), "Test drawing")
            doc.new_page(width=400, height=300)
            doc.save(pdf)
        finding = {"mistake_id": "M01", "mistake_name": "Missing dimensions",
                   "explanation": "Dimensions are missing.", "confidence": "high",
                   "bounding_boxes": [{"coordinate_space": "page_normalized_1000", "box_2d": [10, 20, 100, 200]}]}
        result = {"drawings": [{"page_number": page, "mistakes": [finding]} for page in [1, 2]]}
        (folder / "results.json").write_text(json.dumps(result))
        students.append({"identifier": identifier, "source_pdf": pdf.name,
                         "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()})
    (bundle / "manifest.json").write_text(json.dumps({"id": bundle_id, "label": "Test assignment", "schema_version": 1, "students": students}))
    (bundle / "mistakes_catalog.csv").write_text('ID,Cleaned Mistake Description,Theme,One Example\n1,Missing dimensions,Dimensions,Room width\n')
    (root / "latest.json").write_text(json.dumps({"bundle": bundle_id}))
    return bundle_id


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.old_data = app.DATA
        self.old_imports = app.IMPORTS
        app.DATA = Path(self.tmp.name)
        app.IMPORTS = app.DATA / "inbox"
        self.bundle_id = make_bundle(app.DATA)
        app.init_db()
        self.assertEqual(app.import_latest(), 2)
        with app.connect() as db:
            self.alice = dict(db.execute("SELECT * FROM users WHERE id='alice'").fetchone())
            self.bob = dict(db.execute("SELECT * FROM users WHERE id='bob'").fetchone())
            self.teacher = dict(db.execute("SELECT * FROM users WHERE id='instructor1'").fetchone())
            self.sid = self.bundle_id + ":alice"
            self.finding = dict(db.execute("SELECT * FROM findings WHERE submission_id=? ORDER BY page", (self.sid,)).fetchone())
        with (app.DATA / "private/login_keys.csv").open(newline="") as stream:
            self.keys = {row["identifier"]: row["key"] for row in csv.DictReader(stream)}

    def tearDown(self):
        app.DATA = self.old_data
        app.IMPORTS = self.old_imports
        self.tmp.cleanup()

    def act(self, user, **data):
        with app.connect() as db:
            return app.change_finding(db, user, data)

    def get_finding(self, fid=None):
        with app.connect() as db:
            return dict(db.execute("SELECT * FROM findings WHERE id=?", (fid or self.finding["id"],)).fetchone())

    def rebuttal(self):
        return self.act(self.alice, action="rebuttal", finding=self.finding["id"], version=0, argument="The dimension is shown.")

    def test_reimport_preserves_review_and_credentials(self):
        self.rebuttal()
        self.assertEqual(app.import_latest(), 0)
        self.assertEqual(self.get_finding()["status"], "pending")
        app.init_db()
        with (app.DATA / "private/login_keys.csv").open(newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 4)

    def test_student_isolation(self):
        with app.connect() as db:
            with self.assertRaises(app.Problem) as ctx:
                app.authorize_submission(db, self.bob, self.sid)
            self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(app.Problem):
            self.act(self.bob, action="rebuttal", finding=self.finding["id"], version=0, argument="No")

    def test_rebuttal_cancel_and_resubmit(self):
        self.rebuttal()
        self.act(self.alice, action="cancel", finding=self.finding["id"], version=1)
        self.assertEqual(self.get_finding()["status"], "open")
        self.act(self.alice, action="rebuttal", finding=self.finding["id"], version=2, argument="New evidence")
        self.assertEqual(self.get_finding()["argument"], "New evidence")

    def test_instructor_acceptance_and_stale_decision(self):
        self.rebuttal()
        self.act(self.teacher, action="review", finding=self.finding["id"], version=1, agree=True, feedback="Agreed.")
        row = self.get_finding()
        self.assertEqual((row["status"], row["feedback"], row["reviewer"]), ("accepted", "Agreed.", "instructor1"))
        with self.assertRaises(app.Problem) as ctx:
            self.act(self.teacher, action="review", finding=row["id"], version=1, agree=False, feedback="Other view")
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(app.Problem):
            self.act(self.alice, action="cancel", finding=row["id"], version=2)

    def test_rejection_and_required_comment(self):
        self.rebuttal()
        with self.assertRaises(app.Problem):
            self.act(self.teacher, action="review", finding=self.finding["id"], version=1, agree=False, feedback=" ")
        self.assertEqual(self.get_finding()["status"], "pending")
        self.act(self.teacher, action="review", finding=self.finding["id"], version=1, agree=False, feedback="Please add dimensions.")
        self.assertEqual(self.get_finding()["status"], "rejected")

    def test_student_cannot_review_and_teacher_cannot_rebut(self):
        self.rebuttal()
        with self.assertRaises(app.Problem):
            self.act(self.alice, action="review", finding=self.finding["id"], version=1, agree=True, feedback="Yes")
        with self.assertRaises(app.Problem):
            self.act(self.teacher, action="rebuttal", finding=self.finding["id"], version=1, argument="Yes")

    def test_self_report_replace_box_and_cancel(self):
        fid = self.act(self.alice, action="self_report", submission=self.sid, page=2, description="Missing title", box=[100, 200, 300, 400], catalog_id="M01")["id"]
        self.act(self.alice, action="edit_box", finding=fid, version=0, box=[200, 300, 400, 500])
        row = self.get_finding(fid)
        boxes = json.loads(row["boxes"])
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["box_2d"], [200, 300, 400, 500])
        self.act(self.alice, action="cancel", finding=fid, version=1)
        self.assertEqual(self.get_finding(fid)["status"], "canceled")

    def test_self_report_accept_and_reject(self):
        for agree in [True, False]:
            fid = self.act(self.alice, action="self_report", submission=self.sid, page=1, description="Observation", box=[10, 20, 30, 40])["id"]
            self.act(self.teacher, action="review", finding=fid, version=0, agree=agree, feedback="Decision reason")
            self.assertEqual(self.get_finding(fid)["status"], "accepted" if agree else "rejected")

    def test_box_and_page_validation(self):
        for box in [None, [], [1, 2, 3, 4, 5], [10, 10, 0, 0], [-1, 0, 3, 4], [0, 0, 1001, 2], [0, 0, float("nan"), 2], [False, 0, 3, 4]]:
            with self.subTest(box=box), self.assertRaises(app.Problem):
                self.act(self.alice, action="self_report", submission=self.sid, page=1, description="x", box=box)
        for page in [0, 3, True, "1"]:
            with self.subTest(page=page), self.assertRaises(app.Problem):
                self.act(self.alice, action="self_report", submission=self.sid, page=page, description="x", box=[1, 2, 3, 4])

    def test_scoring_geometric_and_accepted_rebuttal(self):
        findings = [{"kind":"ai", "catalog_id":"M01", "status":"open"} for _ in range(3)]
        findings.append({"kind":"ai", "catalog_id":"M02", "status":"open"})
        scores = app.calculate_scores(findings)
        self.assertEqual(scores["maximum"], 60)
        self.assertEqual(scores["original"], 57.25)
        findings[0]["status"] = "accepted"
        self.assertEqual(app.calculate_scores(findings)["adjusted"], 57.5)
        findings.append({"kind":"self", "catalog_id":"M01", "status":"accepted"})
        findings.append({"kind":"self", "catalog_id":"M01", "status":"accepted"})
        self.assertEqual(app.calculate_scores(findings)["adjusted"], 59)
        self.assertEqual(app.calculate_scores([findings[-1]])["adjusted"], 60)

    def test_roster_validation_is_atomic_and_no_mail_by_default(self):
        roster = app.DATA / "roster.csv"
        roster.write_text("identifier,email\nalice,alice@wisc.edu\nbob,bob@example.com\n")
        with self.assertRaises(ValueError):
            app.roster_import(roster)
        with app.connect() as db:
            self.assertIsNone(db.execute("SELECT email FROM users WHERE id='alice'").fetchone()[0])
        roster.write_text("identifier,email\nalice,alice@wisc.edu\n")
        with patch("app.smtplib.SMTP", side_effect=AssertionError("Unexpected mail")):
            app.roster_import(roster)

    def test_invitation_send_and_idempotence(self):
        roster = app.DATA / "roster.csv"
        roster.write_text("identifier,email\nalice,alice@wisc.edu\n")
        env = {"SMTP_HOST":"smtp.test", "SMTP_USER":"sender", "SMTP_PASSWORD":"fake", "SMTP_FROM":"course@wisc.edu", "PLANA_PUBLIC_URL":"https://plana.example"}
        with patch.dict("os.environ", env), patch("app.smtplib.SMTP") as smtp:
            app.roster_import(roster, True)
            app.roster_import(roster, True)
            messages = smtp.return_value.__enter__.return_value.send_message.call_args_list
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].args[0]["To"], "alice@wisc.edu")
            self.assertIn(self.keys["alice"], messages[0].args[0].get_content())

    def test_http_auth_csrf_private_pages_and_scope(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        def request(method, path, data=None, cookie=None, csrf=None, origin=None):
            conn = http.client.HTTPConnection("127.0.0.1", port)
            headers = {"Content-Type":"application/json"}
            if cookie: headers["Cookie"] = cookie
            if csrf: headers["X-CSRF-Token"] = csrf
            if origin: headers["Origin"] = origin
            conn.request(method, path, body=None if data is None else json.dumps(data), headers=headers)
            response = conn.getresponse()
            output = (response.status, response.read(), response.getheader("Set-Cookie"))
            conn.close()
            return output
        try:
            self.assertEqual(request("GET", "/api/submissions")[0], 401)
            self.assertEqual(request("GET", "/data/private/login_keys.csv")[0], 401)
            self.assertEqual(request("GET", "/")[0], 200)
            login = {"identifier":"alice", "key":self.keys["alice"], "role":"student"}
            self.assertEqual(request("POST", "/api/login", login, origin="https://evil.test")[0], 403)
            self.assertEqual(request("POST", "/api/login", {**login, "key":"wrong"})[0], 401)
            status, body, cookie = request("POST", "/api/login", login)
            self.assertEqual(status, 200)
            cookie = cookie.split(";", 1)[0]
            csrf = json.loads(body)["csrf"]
            response = request("GET", "/api/submissions", cookie=cookie)
            self.assertEqual([row["student_id"] for row in json.loads(response[1])], ["alice"])
            self.assertEqual(request("GET", "/api/submission?submission="+self.bundle_id+":bob", cookie=cookie)[0], 404)
            self.assertEqual(request("GET", "/api/page?submission="+self.bundle_id+":bob&page=1", cookie=cookie)[0], 404)
            response = request("GET", "/api/page?submission="+self.sid+"&page=2", cookie=cookie)
            self.assertEqual(response[0], 200)
            self.assertTrue(response[1].startswith(b"\x89PNG"))
            self.assertEqual(request("GET", "/api/page?submission="+self.sid+"&page=0", cookie=cookie)[0], 400)
            self.assertEqual(request("GET", "/api/pdf?submission="+self.sid, cookie=cookie)[0], 200)
            self.assertEqual(request("POST", "/api/logout", {}, cookie=cookie)[0], 403)
            self.assertEqual(request("POST", "/api/logout", {}, cookie=cookie, csrf=csrf)[0], 200)
            self.assertEqual(request("GET", "/api/me", cookie=cookie)[0], 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


    def test_instructors_can_revise_rebuttal_decisions_and_scores(self):
        self.rebuttal()
        fid = self.finding["id"]
        self.act(self.teacher, action="review", finding=fid, version=1, agree=True, feedback="Accepted initially")
        with app.connect() as db:
            findings = [dict(row) for row in db.execute("SELECT * FROM findings WHERE submission_id=?", (self.sid,))]
            self.assertEqual(app.calculate_scores(findings)["adjusted"], 59)
            other = dict(db.execute("SELECT * FROM users WHERE id='instructor2'").fetchone())
        self.act(other, action="review", finding=fid, version=2, agree=False, feedback="Reconsidered: the dimension is missing")
        row = self.get_finding()
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["reviewer"], "instructor2")
        self.assertEqual(row["argument"], "The dimension is shown.")
        with app.connect() as db:
            findings = [dict(item) for item in db.execute("SELECT * FROM findings WHERE submission_id=?", (self.sid,))]
            self.assertEqual(app.calculate_scores(findings)["adjusted"], 58.5)
        self.act(self.teacher, action="review", finding=fid, version=3, agree=True, feedback="New evidence supports the student")
        self.assertEqual(self.get_finding()["status"], "accepted")
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE finding=? AND action='review'", (fid,)).fetchone()[0], 3)

    def test_self_report_decision_can_change_and_remove_credit(self):
        fid = self.act(self.alice, action="self_report", submission=self.sid, page=1, description="Missing title", box=[10, 20, 30, 40])["id"]
        self.act(self.teacher, action="review", finding=fid, version=0, agree=True, feedback="Valid report")
        with app.connect() as db:
            findings = [dict(row) for row in db.execute("SELECT * FROM findings WHERE submission_id=?", (self.sid,))]
            self.assertEqual(app.calculate_scores(findings)["credit"], 1)
        self.act(self.teacher, action="review", finding=fid, version=1, agree=False, feedback="The title is present on closer inspection")
        row = self.get_finding(fid)
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(len(json.loads(row["boxes"])), 1)
        with app.connect() as db:
            findings = [dict(item) for item in db.execute("SELECT * FROM findings WHERE submission_id=?", (self.sid,))]
            self.assertEqual(app.calculate_scores(findings)["credit"], 0)
        self.act(self.teacher, action="review", finding=fid, version=2, agree=True, feedback="Confirmed after discussion")
        self.assertEqual(self.get_finding(fid)["status"], "accepted")

    def test_decision_edits_require_comment_current_version_and_instructor(self):
        self.rebuttal()
        fid = self.finding["id"]
        self.act(self.teacher, action="review", finding=fid, version=1, agree=True, feedback="Accepted")
        for actor, version, feedback in [(self.alice, 2, "Student change"), (self.teacher, 1, "Stale change"), (self.teacher, 2, " ")]:
            with self.assertRaises(app.Problem):
                self.act(actor, action="review", finding=fid, version=version, agree=False, feedback=feedback)
        self.assertEqual(self.get_finding()["status"], "accepted")
        self.act(self.teacher, action="review", finding=fid, version=2, agree=True, feedback="Clarified feedback only")
        self.assertEqual(self.get_finding()["feedback"], "Clarified feedback only")

    def test_instructors_cannot_review_unsubmitted_or_canceled_requests(self):
        with self.assertRaises(app.Problem):
            self.act(self.teacher, action="review", finding=self.finding["id"], version=0, agree=True, feedback="No request")
        fid = self.act(self.alice, action="self_report", submission=self.sid, page=1, description="Missing label", box=[10, 20, 30, 40])["id"]
        self.act(self.alice, action="cancel", finding=fid, version=0)
        with self.assertRaises(app.Problem):
            self.act(self.teacher, action="review", finding=fid, version=1, agree=True, feedback="Canceled report")


if __name__ == "__main__":
    unittest.main()
