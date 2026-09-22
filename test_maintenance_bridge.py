"""Actual maintenance controller + actual adopter CLI + real temporary Git.
Backup transport, notifier and gateway runtime are explicit fixtures; this is
protocol integration coverage, NOT production backup/reload acceptance.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import test_ffadopt as fixtures
spec=importlib.util.spec_from_file_location('maintenance_bridge_fixture','/home/yuxie1/.hermes/scripts/maintenance.py')
maintenance=importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AdopterTest(methodName='runTest')
        self.f.setUp()
        self.runtime={'sha':self.f.g.base_sha,'reloads':0}
        raw={'repo':str(self.f.g.live),'python':sys.executable,
             'updater':str(HERE/'ffadopt.py'),'manifest':str(self.f.cfg_path),
             'restart_state_file':str(self.f.state_file),
             'backup_command':['/bin/sh','-c','printf "BACKUP_VERIFIED https://github.com/YuuuXie/chilicraft/releases/tag/fixture bytes=123\\n"'],
             'notify_command':['/bin/true'],'log_root':str(self.f.root/'logs'),
             'receipt_dir':str(self.f.root/'maintenance-receipts'),
             'lock_file':str(self.f.root/'maintenance.lock'),
             'services':[{'unit':'fixture.service','home':str(self.f.root/'home')}],
             'hermes_home':str(self.f.root/'home')}
        self.raw=raw
    def tearDown(self): self.f.tearDown()
    def run_worker(self):
        runtime=self.runtime
        class Worker(maintenance.Maintenance):
            def read_service(self,unit,home): return {'service_pid':42,'running_sha':runtime['sha']}
            def verified(self,before,after,sha,require_new_pid=False): return after['running_sha']==sha
            def stage_reload(self,sha):
                runtime.update(sha=sha,reloads=runtime['reloads']+1)
                self.data['services']={'fixture.service':{'service_pid':43,'running_sha':sha}}
                return {'ok':True,'verified':['fixture.service'],'pending':[],'reason':None}
        w=Worker(maintenance.Config(self.raw))
        with patch.dict(os.environ,{'FFADOPT_CONFIG':str(self.f.cfg_path)}):
            try:
                rc=w.run()
            finally:
                if w._log_fh is not None:
                    w._log_fh.close()
        return rc,w
    def test_no_ready_only_backup_no_reload(self):
        self.f.ready_path.unlink()
        rc,w=self.run_worker()
        self.assertEqual(rc,0)
        self.assertEqual(self.f.head(),self.f.g.base_sha)
        self.assertEqual(self.runtime['reloads'],0)
        self.assertTrue(w.data['stages']['backup']['verified'])
    def test_failed_backup_blocks_valid_candidate(self):
        self.raw['backup_command']=['/bin/sh','-c','exit 9']
        rc,w=self.run_worker()
        self.assertEqual(rc,maintenance.EXIT_BACKUP_FAILED)
        self.assertEqual(self.f.head(),self.f.g.base_sha)
        self.assertNotIn('update',w.data['stages'])
        self.assertEqual(self.runtime['reloads'],0)
    def test_first_adoption_then_stable_noop(self):
        rc,w=self.run_worker()
        self.assertEqual(rc,0)
        self.assertEqual(self.f.head(),self.f.g.cand_sha)
        self.assertFalse(self.f.state_file.exists())
        self.assertEqual(self.runtime['reloads'],1)
        rc2,w2=self.run_worker()
        self.assertEqual(rc2,0)
        self.assertEqual(self.runtime['reloads'],1)
        self.assertEqual(w2.data['outcome'],'noop')
    def test_failed_receipt_preserves_backup_but_blocks_update(self):
        d=json.loads(self.f.receipt.read_text());d['status']='aborted';d['exit_code']=4
        self.f.receipt.write_text(json.dumps(d))
        self.f.write_ready(receipt_sha256=fixtures.sha256(self.f.receipt.read_bytes()))
        rc,w=self.run_worker()
        self.assertEqual(rc,maintenance.EXIT_UPDATE_FAILED)
        self.assertTrue(w.data['stages']['backup']['verified'])
        self.assertEqual(self.f.head(),self.f.g.base_sha)
        self.assertEqual(self.runtime['reloads'],0)

if __name__=='__main__': unittest.main(verbosity=2)
