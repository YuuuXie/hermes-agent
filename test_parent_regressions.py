"""Parent-discovered pre-wiring regressions; real Git, fixture control files."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import ffadopt
import test_ffadopt as fixtures

class ParentRegressions(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AdopterTest(methodName='runTest');self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def test_readback_failure_after_ff_is_not_untouched_and_has_marker(self):
        original=ffadopt.git;state={'merged':False}
        def git(repo,*args,**kwargs):
            if state['merged'] and args==('rev-parse','HEAD'):
                ffadopt.refuse('controlled_post_merge_readback_failure')
            result=original(repo,*args,**kwargs)
            if args[:2]==('merge','--ff-only'):state['merged']=True
            return result
        output=io.StringIO()
        with patch.object(ffadopt,'git',git),contextlib.redirect_stdout(output):
            self.assertEqual(self.f.apply(),ffadopt.EXIT_REFUSED)
        self.assertEqual(self.f.head(),self.f.g.cand_sha)
        self.assertTrue(self.f.state_file.exists())
        self.assertIn('production MAY have changed',output.getvalue())
        self.assertNotIn('production untouched',output.getvalue())
    def test_marker_io_failure_after_ff_is_reported_and_recoverable(self):
        output=io.StringIO()
        with patch.object(ffadopt,'write_marker',side_effect=OSError('controlled marker I/O failure')),contextlib.redirect_stdout(output):
            self.assertEqual(self.f.apply(),ffadopt.EXIT_REFUSED)
        self.assertEqual(self.f.head(),self.f.g.cand_sha)
        self.assertIn('production MAY have changed',output.getvalue())
        self.assertEqual(self.f.apply(),11)
        self.assertTrue(self.f.state_file.exists())
    def test_clear_accepts_equivalent_resolved_marker_path(self):
        self.assertEqual(self.f.apply(),11)
        alias=self.f.root/'state-alias';alias.symlink_to(self.f.state_file.parent,target_is_directory=True)
        rc=ffadopt.main(['restart-clear','--config',str(self.f.cfg_path),'--state-file',str(alias/self.f.state_file.name),'--sha',self.f.g.cand_sha])
        self.assertEqual(rc,0)
        self.assertEqual(self.f.apply(),0)
    def test_absent_marker_does_not_create_false_runtime_ack(self):
        rc=ffadopt.main(['restart-clear','--config',str(self.f.cfg_path),'--state-file',str(self.f.state_file),'--sha',self.f.g.cand_sha])
        self.assertEqual(rc,ffadopt.EXIT_REFUSED)
        cfg=json.loads(self.f.cfg_path.read_text())
        self.assertFalse(ffadopt.Journal(Path(cfg['applied_journal'])).is_cleared(self.f.g.cand_sha))

if __name__=='__main__':unittest.main(verbosity=2)
