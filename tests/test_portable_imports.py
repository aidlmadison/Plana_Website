"""Portable evaluation imports, durable review state, and ZIP handoffs."""
import hashlib
import http.client
import io
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import fitz
import run_bundle
import run_imports


def make_run(root, name='evaluation_2026_10_21_12_00', note=None):
    folder = root / name
    (folder / 'input').mkdir(parents=True)
    (folder / 'reference').mkdir()
    with fitz.open() as pdf:
        pdf.new_page()
        pdf.save(folder / 'input/alice_123_assignment.pdf')
    result = {'source_pdf': 'alice_123_assignment.pdf', 'notes': note, 'drawings': [
        {'page_number': 1, 'mistakes': [{'mistake_id': 'M01', 'mistake_name': 'Missing dimension',
         'explanation': 'Please dimension the room.', 'confidence': 'high', 'bounding_boxes': []}]}]}
    (folder / 'results.json').write_text(json.dumps([result]))
    (folder / 'reference/mistakes_catalog.csv').write_text('ID,Cleaned Mistake Description,Theme,One Example\n1,Missing dimensions,Dimensions,Room width\n')
    run_bundle.finalize_run(folder)
    return folder


class PortableImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.old_data, self.old_imports = app.DATA, app.IMPORTS
        app.DATA, app.IMPORTS = self.root / 'private', self.root / 'inbox'
        app.init_db()

    def tearDown(self):
        app.DATA, app.IMPORTS = self.old_data, self.old_imports
        self.temporary.cleanup()

    def test_unchanged_folder_and_second_run_need_no_pointer(self):
        run = make_run(app.IMPORTS)
        before = {p.relative_to(run):p.read_bytes() for p in run.rglob('*') if p.is_file()}
        self.assertEqual(app.refresh_imports()['imported'], 1)
        self.assertEqual(before, {p.relative_to(run):p.read_bytes() for p in run.rglob('*') if p.is_file()})
        self.assertFalse((app.DATA / 'latest.json').exists())
        with app.connect() as db:
            self.assertEqual(app.read_catalog(db, run_bundle.inspect_run(run)['id'])[0]['id'], 'M01')
        make_run(app.IMPORTS, 'evaluation_2026_10_22_12_00', 'A different evaluation')
        self.assertEqual(app.refresh_imports()['imported'], 1)
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM assignments').fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0], 1)

    def test_relocation_and_reimport_preserve_key_reviews_and_pdf(self):
        run = make_run(app.IMPORTS)
        app.refresh_imports()
        with app.connect() as db:
            key = db.execute("SELECT hash FROM users WHERE id='alice'").fetchone()[0]
            db.execute("UPDATE findings SET status='accepted',argument='It is shown',feedback='Agreed',version=4")
            before = dict(db.execute('SELECT * FROM findings').fetchone())
        shutil.copytree(run, app.IMPORTS / 'evaluation_renamed')
        shutil.rmtree(run)
        self.assertEqual(app.refresh_imports(), {'imported': 0, 'reused': 1, 'errors': []})
        shutil.rmtree(app.IMPORTS)
        with app.connect() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM findings').fetchone()), before)
            self.assertEqual(db.execute("SELECT hash FROM users WHERE id='alice'").fetchone()[0], key)
            self.assertTrue(Path(db.execute('SELECT pdf FROM submissions').fetchone()[0]).is_file())

    def test_legacy_to_original_folder_preserves_existing_assignment(self):
        run = make_run(app.IMPORTS)
        info = run_bundle.inspect_run(run)
        legacy = app.DATA / 'imports' / info['id']
        target = legacy / 'students/alice'
        target.mkdir(parents=True)
        shutil.copyfile(run / 'input/alice_123_assignment.pdf', target / 'alice_123_assignment.pdf')
        result = info['students'][0]['result']
        (target / 'results.json').write_text(json.dumps(result))
        shutil.copyfile(run / 'results.json', legacy / 'results.json')
        shutil.copyfile(run / 'reference/mistakes_catalog.csv', legacy / 'mistakes_catalog.csv')
        (legacy / 'manifest.json').write_text(json.dumps({'id': info['id'], 'label': run.name, 'schema_version':1,
            'students':[{'identifier':'alice','source_pdf':'alice_123_assignment.pdf','pdf_sha256':info['students'][0]['pdf_sha256']}]}))
        self.assertEqual(run_imports.import_run(legacy, app.DATA, app.connect, app.create_user), 1)
        with app.connect() as db:
            db.execute("UPDATE findings SET status='pending',argument='Review me',version=1")
        self.assertEqual(run_imports.import_run(run, app.DATA, app.connect, app.create_user), 0)
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM assignments').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT argument FROM findings').fetchone()[0], 'Review me')

    def test_partial_and_changed_runs_do_not_import(self):
        run = make_run(app.IMPORTS)
        shutil.copyfile(run/'input/alice_123_assignment.pdf',run/'input/bob_456_assignment.pdf')
        report = app.refresh_imports()
        self.assertEqual(report['imported'],0)
        self.assertIn('incomplete',report['errors'][0]['error'])
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0],0)
        (run/'input/bob_456_assignment.pdf').unlink()
        (run/'results.json').write_text((run/'results.json').read_text()+' ')
        self.assertIn('changed after finalization',app.refresh_imports()['errors'][0]['error'])

    def test_zip_roundtrip_and_repeated_upload(self):
        run = make_run(self.root/'runs')
        data = run_bundle.archive_run(run).read_bytes()
        destination = run_imports.receive_archive(io.BytesIO(data),len(data),app.IMPORTS,run.name+'.zip')
        self.assertEqual(run_bundle.inspect_run(destination)['fingerprint'],run_bundle.inspect_run(run)['fingerprint'])
        self.assertEqual(run_imports.receive_archive(io.BytesIO(data),len(data),app.IMPORTS,run.name+'.zip'),destination)
        self.assertEqual(app.refresh_imports()['imported'],1)

    def test_zip_rejects_unsafe_paths_and_truncated_upload(self):
        for filename in ['../outside.txt','/outside.txt','C:/outside.txt','run/../outside.txt','run\\outside.txt']:
            payload = io.BytesIO()
            with zipfile.ZipFile(payload,'w') as archive:
                archive.writestr(filename,b'bad')
            data=payload.getvalue()
            with self.assertRaises(ValueError):
                run_imports.receive_archive(io.BytesIO(data),len(data),app.IMPORTS,'evaluation.zip')
        with self.assertRaises(ValueError):
            run_imports.receive_archive(io.BytesIO(b'ab'),100,app.IMPORTS,'evaluation.zip')
        self.assertFalse((self.root/'outside.txt').exists())

    def test_http_zip_requires_instructor_and_csrf(self):
        run=make_run(self.root/'runs')
        data=run_bundle.archive_run(run).read_bytes()
        # Create a student account without importing the uploaded run yet.
        with app.connect() as db:
            app.create_user(db,'alice','student')
            for identifier in ['alice','instructor1']:
                db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(hashlib.sha256((identifier+'token').encode()).hexdigest(),identifier,identifier+'csrf',9999999999))
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def upload(identifier,csrf=True):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1])
            headers={'Cookie':'plana_session='+identifier+'token','Content-Type':'application/zip','X-File-Name':run.name+'.zip'}
            if csrf: headers['X-CSRF-Token']=identifier+'csrf'
            conn.request('POST','/api/import-run',body=data,headers=headers)
            response=conn.getresponse();status=response.status;body=response.read();conn.close()
            return status,json.loads(body)
        try:
            self.assertEqual(upload('instructor1',False)[0],403)
            self.assertEqual(upload('alice')[0],403)
            status,body=upload('instructor1')
            self.assertEqual(status,200);self.assertEqual(body['imported'],1);self.assertEqual(body['errors'],[])
            self.assertEqual(upload('instructor1')[1]['imported'],0)
        finally:
            server.shutdown();server.server_close();thread.join()
