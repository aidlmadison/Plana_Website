"""Instructor batch visibility protects listings, files, and student actions."""
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from test_app import make_bundle


class BatchVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary=TemporaryDirectory()
        self.old_data,self.old_imports=app.DATA,app.IMPORTS
        app.DATA=Path(self.temporary.name);app.IMPORTS=app.DATA/'inbox'
        self.first=make_bundle(app.DATA)
        self.second='evaluation-'+'b'*20
        second=app.DATA/'imports'/self.second
        shutil.copytree(app.DATA/'imports'/self.first,second)
        manifest=json.loads((second/'manifest.json').read_text());manifest.update(id=self.second,label='Second batch')
        (second/'manifest.json').write_text(json.dumps(manifest))
        app.init_db();self.assertEqual(app.refresh_imports()['imported'],4)
        with app.connect() as db:
            self.teacher=dict(db.execute("SELECT * FROM users WHERE id='instructor1'").fetchone())
            self.student=dict(db.execute("SELECT * FROM users WHERE id='alice'").fetchone())
            for identifier in ['instructor1','alice']:
                db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                    (hashlib.sha256(identifier.encode()).hexdigest(),identifier,identifier+'csrf',9999999999))
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        app.DATA,app.IMPORTS=self.old_data,self.old_imports;self.temporary.cleanup()

    def request(self,method,path,role='alice',data=None,csrf=True):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1])
        headers={'Cookie':'plana_session='+role,'Content-Type':'application/json'}
        if csrf:headers['X-CSRF-Token']=role+'csrf'
        conn.request(method,path,body=None if data is None else json.dumps(data),headers=headers)
        response=conn.getresponse();status=response.status;body=response.read();conn.close()
        return status,body

    def set_visibility(self,visible,version=0):
        with app.connect() as db:
            return app.set_batch_visibility(db,self.teacher,{'assignment':self.first,'visible':visible,'version':version})

    def test_hidden_batch_blocks_listing_pdf_page_details_and_edits(self):
        self.assertEqual(len(json.loads(self.request('GET','/api/submissions')[1])),2)
        status,_=self.request('POST','/api/batches/visibility','instructor1',{'assignment':self.first,'visible':False,'version':0})
        self.assertEqual(status,200)
        rows=json.loads(self.request('GET','/api/submissions')[1]);self.assertEqual([r['assignment_id'] for r in rows],[self.second])
        sid=self.first+':alice'
        for path in ['/api/submission?submission='+sid,'/api/pdf?submission='+sid,'/api/page?submission='+sid+'&page=1']:
            self.assertEqual(self.request('GET',path)[0],404,path)
            self.assertEqual(self.request('GET',path,'instructor1')[0],200,path)
        self.assertEqual(self.request('POST','/api/action',data={'action':'rebuttal','finding':sid+':0:0','version':0,'argument':'Shown on drawing'})[0],404)
        self.assertEqual(len(json.loads(self.request('GET','/api/submissions','instructor1')[1])),4)

    def test_reimport_restart_and_restore_preserve_keys_and_reviews(self):
        with app.connect() as db:
            db.execute("UPDATE findings SET status='accepted',argument='A rebuttal',feedback='Agreed',reviewer='instructor1',version=2 WHERE submission_id=?",(self.first+':alice',))
            key=db.execute("SELECT hash FROM users WHERE id='alice'").fetchone()[0]
            findings=[tuple(row) for row in db.execute('SELECT * FROM findings ORDER BY id')]
        self.set_visibility(False)
        self.assertEqual(app.refresh_imports()['imported'],0)
        app.init_db()
        with app.connect() as db:
            self.assertFalse(next(row for row in app.batches(db,self.teacher) if row['id']==self.first)['visible'])
        self.set_visibility(True,1)
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT hash FROM users WHERE id='alice'").fetchone()[0],key)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM findings ORDER BY id')],findings)
        self.assertEqual(len(json.loads(self.request('GET','/api/submissions')[1])),2)

    def test_visibility_requires_instructor_csrf_and_current_version(self):
        payload={'assignment':self.first,'visible':False,'version':0}
        self.assertEqual(self.request('GET','/api/batches')[0],403)
        self.assertEqual(self.request('POST','/api/batches/visibility',data=payload)[0],403)
        self.assertEqual(self.request('POST','/api/batches/visibility','instructor1',payload,False)[0],403)
        self.set_visibility(False)
        self.assertEqual(self.request('POST','/api/batches/visibility','instructor1',payload)[0],409)
        self.assertEqual(self.request('POST','/api/batches/visibility','instructor1',{**payload,'visible':'yes','version':1})[0],400)
        with app.connect() as db:
            app.set_batch_visibility(db,self.teacher,{'assignment':self.second,'visible':False,'version':0})
        self.assertEqual(json.loads(self.request('GET','/api/submissions')[1]),[])
