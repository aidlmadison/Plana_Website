"""Permanent batch deletion frees managed copies and preserves unrelated data."""
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import batch_storage
import test_batches


class BatchDeletionTests(unittest.TestCase):
    setUp=test_batches.BatchVisibilityTests.setUp
    tearDown=test_batches.BatchVisibilityTests.tearDown
    request=test_batches.BatchVisibilityTests.request

    def preview(self):
        status,body=self.request('GET','/api/batches/delete-preview?assignment='+self.first,'instructor1')
        self.assertEqual(status,200,body)
        return json.loads(body)

    def payload(self):
        return {'assignment':self.first,'token':self.preview()['token'],'confirmation':'DELETE'}

    def test_delete_removes_all_copies_and_reviews_but_preserves_other_batch_and_keys(self):
        with app.connect() as db:
            users=[tuple(row) for row in db.execute('SELECT * FROM users ORDER BY id')]
            other=[tuple(row) for row in db.execute('SELECT * FROM findings WHERE submission_id LIKE ? ORDER BY id',(self.second+':%',))]
            teacher=dict(self.teacher)
            db.execute("UPDATE findings SET status='accepted',feedback='Instructor feedback',version=2 WHERE submission_id=?",(self.first+':alice',))
        preview=self.preview();self.assertGreater(preview['bytes'],0);self.assertEqual(preview['copies'],2)
        status,body=self.request('POST','/api/batches/delete','instructor1',{'assignment':self.first,'token':preview['token'],'confirmation':'DELETE'})
        self.assertEqual(status,200,body);self.assertTrue(json.loads(body)['deleted']);self.assertFalse(json.loads(body)['cleanup_pending'])
        for root in app.batch_roots():self.assertFalse((root/self.first).exists())
        self.assertTrue((app.DATA/'evaluations'/self.second).is_dir())
        self.assertEqual(app.refresh_imports(),{'imported':0,'reused':1,'errors':[]})
        with app.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM users ORDER BY id')],users)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM findings ORDER BY id')],other)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0],2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='batch_deleted'").fetchone()[0],1)
        self.assertEqual(self.request('GET','/api/pdf?submission='+self.first+':alice','instructor1')[0],404)
        self.assertEqual(self.request('GET','/api/pdf?submission='+self.second+':alice')[0],200)

    def test_role_csrf_confirmation_and_stale_preview_are_enforced(self):
        payload=self.payload()
        self.assertEqual(self.request('GET','/api/batches/delete-preview?assignment='+self.first)[0],403)
        self.assertEqual(self.request('POST','/api/batches/delete',data=payload)[0],403)
        self.assertEqual(self.request('POST','/api/batches/delete','instructor1',payload,False)[0],403)
        self.assertEqual(self.request('POST','/api/batches/delete','instructor1',{**payload,'confirmation':''})[0],400)
        with app.connect() as db:db.execute('UPDATE findings SET version=version+1 WHERE submission_id=?',(self.first+':alice',))
        self.assertEqual(self.request('POST','/api/batches/delete','instructor1',payload)[0],409)
        self.assertTrue((app.DATA/'evaluations'/self.first).is_dir())

    def test_deliberate_reupload_works_and_does_not_rotate_accounts(self):
        original=app.DATA/'source-original';shutil.copytree(app.DATA/'imports'/self.first,original)
        with app.connect() as db:key=db.execute("SELECT hash FROM users WHERE id='alice'").fetchone()[0]
        self.assertEqual(self.request('POST','/api/batches/delete','instructor1',self.payload())[0],200)
        self.assertTrue(original.is_dir())
        shutil.copytree(original,app.DATA/'imports'/self.first)
        self.assertEqual(app.refresh_imports()['imported'],2)
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT hash FROM users WHERE id='alice'").fetchone()[0],key)
            self.assertTrue(all(row[0]=='open' for row in db.execute('SELECT status FROM findings WHERE submission_id=?',(self.first+':alice',))))

    def test_external_or_shared_paths_are_never_deleted(self):
        external=app.DATA/'outside-managed-batches';external.mkdir();(external/'keep.txt').write_text('Keep me')
        with app.connect() as db:db.execute('UPDATE assignments SET bundle=? WHERE id=?',(str(external),self.first))
        status,_=self.request('GET','/api/batches/delete-preview?assignment='+self.first,'instructor1')
        self.assertEqual(status,409);self.assertEqual((external/'keep.txt').read_text(),'Keep me')
        with app.connect() as db:db.execute('UPDATE assignments SET bundle=?',(str(app.DATA/'evaluations'/self.first),))
        self.assertEqual(self.request('GET','/api/batches/delete-preview?assignment='+self.first,'instructor1')[0],409)

    def test_staging_failure_restores_files_and_database(self):
        payload=self.payload();original_rename=Path.rename;failed=False
        def fail_second(path,target):
            nonlocal failed
            if path==app.DATA/'imports'/self.first and not failed:
                failed=True;raise OSError('Synthetic busy directory')
            return original_rename(path,target)
        with app.connect() as db, patch.object(Path,'rename',fail_second), self.assertRaises(OSError):
            batch_storage.delete(db,self.teacher,payload,app.DATA,app.batch_roots())
        self.assertTrue((app.DATA/'evaluations'/self.first).is_dir());self.assertTrue((app.DATA/'imports'/self.first).is_dir())
        with app.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM assignments').fetchone()[0],2)

    def test_failed_cleanup_can_be_retried_without_resurrecting_batch(self):
        payload=self.payload()
        with patch('batch_storage.remove_tree',side_effect=OSError('Synthetic busy files')):
            status,body=self.request('POST','/api/batches/delete','instructor1',payload)
        self.assertEqual(status,200);self.assertTrue(json.loads(body)['cleanup_pending'])
        self.assertEqual(app.refresh_imports()['imported'],0)
        status,body=self.request('POST','/api/batches/cleanup','instructor1',{})
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['pending'],0)
        self.assertEqual(list((app.DATA/'.deleting').iterdir()),[])

    def test_restart_recovery_restores_uncommitted_file_moves(self):
        source=app.DATA/'evaluations'/self.first
        job=app.DATA/'.deleting'/'interrupted';job.mkdir(parents=True)
        (job/'plan.json').write_text(json.dumps({'assignment':self.first,'folders':[str(source)]}))
        source.rename(job/'0')
        with app.connect() as db:result=batch_storage.recover(db,app.DATA,app.batch_roots())
        self.assertEqual(result,{'pending':0});self.assertTrue(source.is_dir());self.assertFalse(job.exists())
