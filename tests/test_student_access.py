"""Roster validation, invitation isolation, and safe delivery retries."""
import json
import csv
import io
import http.client
import threading
import unittest
from unittest.mock import patch

import test_app
import app
import student_access as access


MAIL = {"SMTP_HOST":"smtp.test", "SMTP_USER":"course", "SMTP_PASSWORD":"fake",
        "SMTP_FROM":"course@wisc.edu", "PLANA_PUBLIC_URL":"https://plana.example"}


class StudentAccessTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown

    def rows(self, identifiers=("alice", "bob")):
        with app.connect() as db:
            students = {row["identifier"]:row for row in access.overview(db, self.teacher)["students"]}
        return [{"identifier":identifier, "name":identifier.title()+" Student", "email":identifier+"@wisc.edu",
                 "version":students[identifier]["version"]} for identifier in identifiers]

    def save(self, rows=None):
        with app.connect() as db:
            return access.save_roster(db, self.teacher, rows or self.rows())

    def recipients(self, identifiers=("alice", "bob")):
        with app.connect() as db:
            students = access.overview(db, self.teacher)["students"]
        return [{key:row[key] for key in ("identifier", "name", "email", "version")} | {"reviewed":True}
                for row in students if row["identifier"] in identifiers]

    def test_preview_matches_pdf_identifiers_and_does_not_write(self):
        with app.connect() as db:
            preview = access.preview_roster(db, self.teacher, "identifier,name,email\nalice,Alice Student,alice@wisc.edu\n")
            self.assertEqual(preview["errors"], [])
            self.assertEqual(preview["rows"][0]["name"], "Alice Student")
            self.assertIsNone(db.execute("SELECT email FROM users WHERE id='alice'").fetchone()[0])
            overview = access.overview(db, self.teacher)
            self.assertEqual(overview["students"][0]["pdfs"], ["alice_assignment.pdf"])
            self.assertNotIn("access_key", overview["students"][0])

    def test_preview_flags_unknown_missing_names_and_duplicate_emails(self):
        fixtures = ["identifier,name,email\nunknown,Name,x@wisc.edu\n",
                    "identifier,name,email\nalice,,alice@wisc.edu\n",
                    "identifier,name,email\nalice,Alice,x@wisc.edu\nbob,Bob,x@wisc.edu\n",
                    "identifier,name,email\nalice,Alice,x@example.com\n"]
        with app.connect() as db:
            for value in fixtures:
                self.assertTrue(access.preview_roster(db, self.teacher, value)["errors"])

    def test_save_is_atomic_checks_duplicates_and_versions(self):
        rows = self.rows()
        rows[1]["email"] = rows[0]["email"]
        with self.assertRaises(access.AccessError):
            self.save(rows)
        with app.connect() as db:
            self.assertIsNone(db.execute("SELECT email FROM users WHERE id='alice'").fetchone()[0])
        old = self.rows()
        self.save(old)
        with self.assertRaises(access.AccessError) as error:
            self.save(old)
        self.assertEqual(error.exception.status, 409)

    def test_existing_keys_are_preserved(self):
        with app.connect() as db:
            result = access.generate_keys(db, self.teacher, app.DATA, app.password_hash)
            self.assertEqual(result["generated"], 0)
            self.assertEqual(result["keys"]["alice"], self.keys["alice"])

    def test_missing_keys_can_be_generated_and_validate_at_login(self):
        with app.connect() as db:
            db.execute("UPDATE users SET access_key=NULL WHERE id='alice'")
        with app.connect() as db:
            result = access.generate_keys(db, self.teacher, app.DATA, app.password_hash)
            self.assertEqual(result["generated"], 1)
            row = db.execute("SELECT * FROM users WHERE id='alice'").fetchone()
            self.assertEqual(app.password_hash(result["keys"]["alice"], row["salt"]), row["hash"])

    def test_unreviewed_and_stale_snapshots_never_send(self):
        self.save()
        entries = self.recipients()
        entries[0]["reviewed"] = False
        with patch.dict("os.environ", MAIL), patch("student_access.smtplib.SMTP") as smtp:
            with self.assertRaises(access.AccessError):
                access.send_invitations(app.connect, self.teacher, entries)
            entries[0]["reviewed"] = True
            entries[0]["name"] = "Wrong name"
            with self.assertRaises(access.AccessError):
                access.send_invitations(app.connect, self.teacher, entries)
            smtp.assert_not_called()
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM invitations").fetchone()[0], 0)

    def test_sends_named_email_once_and_records_success(self):
        self.save()
        entries = self.recipients()
        with patch.dict("os.environ", MAIL), patch("student_access.smtplib.SMTP") as smtp:
            transport = smtp.return_value.__enter__.return_value
            transport.send_message.return_value = {}
            result = access.send_invitations(app.connect, self.teacher, entries)
            repeated = access.send_invitations(app.connect, self.teacher, entries)
            self.assertEqual(result["sent"], ["alice", "bob"])
            self.assertEqual(repeated["skipped"], ["alice", "bob"])
            self.assertEqual(transport.send_message.call_count, 2)
            message = transport.send_message.call_args_list[0].args[0]
            self.assertEqual(message["To"], "alice@wisc.edu")
            self.assertIn("Hello Alice Student", message.get_content())
            self.assertIn(self.keys["alice"], message.get_content())
        with app.connect() as db:
            self.assertEqual(access.overview(db, self.teacher)["students"][0]["status"], "sent")

    def test_failed_delivery_can_retry_without_resending_successes(self):
        self.save()
        entries = self.recipients()
        with patch.dict("os.environ", MAIL), patch("student_access.smtplib.SMTP") as smtp:
            transport = smtp.return_value.__enter__.return_value
            transport.send_message.side_effect = [{}, access.smtplib.SMTPException("rejected")]
            result = access.send_invitations(app.connect, self.teacher, entries)
            self.assertEqual(result["sent"], ["alice"])
            self.assertEqual(result["failed"], ["bob"])
            transport.send_message.side_effect = None
            transport.send_message.return_value = {}
            retried = access.send_invitations(app.connect, self.teacher, entries)
            self.assertEqual(retried["sent"], ["bob"])
            self.assertEqual(retried["skipped"], ["alice"])

    def test_changed_recipient_requires_a_new_key(self):
        self.save()
        with app.connect() as db:
            db.execute("UPDATE users SET invited_at=1,last_sent_email=email,key_email=email WHERE id='alice'")
        rows = self.rows(("alice",))
        rows[0]["email"] = "corrected@wisc.edu"
        self.save(rows)
        with app.connect() as db:
            self.assertFalse(access.overview(db, self.teacher)["students"][0]["has_key"])
            result = access.generate_keys(db, self.teacher, app.DATA, app.password_hash)
            self.assertEqual(result["generated"], 1)
            self.assertNotEqual(result["keys"]["alice"], self.keys["alice"])
            self.assertTrue(access.overview(db, self.teacher)["students"][0]["has_key"])

    def test_pending_send_blocks_roster_changes(self):
        self.save()
        with app.connect() as db:
            row = db.execute("SELECT * FROM users WHERE id='alice'").fetchone()
            db.execute("INSERT INTO invitations(user_id,roster_version,key_version,status) VALUES (?,?,?,'sending')",
                       (row["id"],row["roster_version"],row["key_version"]))
        with self.assertRaises(access.AccessError):
            self.save(self.rows(("alice",)))

    def test_email_configuration_blocks_unconfigured_delivery(self):
        self.save()
        with patch.dict("os.environ", {}, clear=True), patch("student_access.smtplib.SMTP") as smtp:
            with self.assertRaises(access.AccessError):
                access.send_invitations(app.connect, self.teacher, self.recipients())
            smtp.assert_not_called()

    def test_student_cannot_access_roster_or_credentials_over_http(self):
        server = app.ThreadingHTTPServer(("127.0.0.1",0), app.Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        def request(method, path, data=None, cookie=None, csrf=None):
            conn = http.client.HTTPConnection("127.0.0.1",server.server_address[1])
            headers={"Content-Type":"application/json"}
            if cookie: headers["Cookie"]=cookie
            if csrf: headers["X-CSRF-Token"]=csrf
            conn.request(method,path,None if data is None else json.dumps(data),headers)
            response=conn.getresponse()
            result=response.status,response.read(),response.getheader("Set-Cookie")
            conn.close()
            return result
        try:
            self.assertEqual(request("GET","/api/access")[0],401)
            status,body,cookie=request("POST","/api/login",{"identifier":"alice","role":"student","key":self.keys["alice"]})
            self.assertEqual(status,200)
            csrf=json.loads(body)["csrf"]
            cookie=cookie.split(";",1)[0]
            self.assertEqual(request("GET","/api/access",cookie=cookie)[0],403)
            self.assertEqual(request("GET","/api/credentials",cookie=cookie)[0],403)
            self.assertEqual(request("GET","/api/credentials.csv",cookie=cookie)[0],403)
            for route in ["keys","send","roster-preview","roster-save"]:
                self.assertEqual(request("POST","/api/access/"+route,{},cookie,csrf)[0],403)
            status,body,cookie=request("POST","/api/login",{"identifier":"instructor1","role":"instructor","key":self.keys["instructor1"]})
            self.assertEqual(status,200)
            csrf=json.loads(body)["csrf"]
            cookie=cookie.split(";",1)[0]
            self.assertEqual(request("POST","/api/access/keys",{},cookie)[0],403)
            status,body,_=request("GET","/api/access",cookie=cookie)
            self.assertEqual(status,200)
            self.assertEqual(len(json.loads(body)["students"]),2)
            status,body,_=request("POST","/api/access/keys",{},cookie,csrf)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["keys"]["alice"],self.keys["alice"])
            status,body,_=request("GET","/api/credentials",cookie=cookie)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["students"][0]["key"],self.keys["alice"])
            status,body,_=request("GET","/api/credentials.csv",cookie=cookie)
            self.assertEqual(status,200)
            self.assertIn(self.keys["alice"],body.decode("utf-8-sig"))
            self.assertEqual(request("POST","/api/access/send",{},cookie,csrf)[0],410)
        finally:
            server.shutdown();server.server_close();thread.join()


    def test_manual_credentials_csv_preserves_roster_and_login_keys(self):
        self.save()
        with app.connect() as db:
            output = access.credentials(db, self.teacher)
            self.assertEqual(output["students"][0]["email"], "alice@wisc.edu")
            self.assertEqual(output["students"][0]["name"], "Alice Student")
            self.assertEqual(output["students"][0]["key"], self.keys["alice"])
            data = access.credentials_csv(db, self.teacher)
            rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
            self.assertEqual(rows[0], {"identifier":"alice", "name":"Alice Student", "email":"alice@wisc.edu", "key":self.keys["alice"]})
            access.write_credentials_export(db, app.DATA)
        self.assertEqual((app.DATA / "private/student_credentials.csv").read_bytes(), data)

    def test_manual_export_keeps_students_without_emails_and_denies_students(self):
        with app.connect() as db:
            rows = access.credentials(db, self.teacher)["students"]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["email"], "")
            self.assertEqual(rows[0]["key"], self.keys["alice"])
            with self.assertRaises(access.AccessError):
                access.credentials(db, self.alice)
            with self.assertRaises(access.AccessError):
                access.credentials_csv(db, self.alice)


if __name__ == "__main__":
    unittest.main()
