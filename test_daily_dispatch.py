import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import daily_dispatch as d

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def enable(self):
        (self.root/'adoption.json').write_text(json.dumps({'enabled':True}))
    def test_missing_config_falls_back_to_backup(self):
        self.assertEqual(d.route(self.root)['mode'],'backup_only')
    def test_no_ready_has_no_process_or_network_work(self):
        self.enable()
        with patch.object(d.subprocess,'run',side_effect=AssertionError('must stay cheap')):
            self.assertEqual(d.route(self.root)['mode'],'backup_only')
    def test_ready_uses_only_new_external_config(self):
        self.enable();(self.root/'ready.json').write_text('{}')
        with patch.object(d.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as call:
            r=d.route(self.root)
        self.assertEqual(r['mode'],'backup_then_adopt')
        self.assertEqual(r['argv'][-1],str(self.root/'maintenance-config.json'))
        self.assertIn('check-config',call.call_args.args[0])
        self.assertNotIn('apply',r['argv'])
    def test_bad_adoption_config_cannot_block_backup(self):
        self.enable();(self.root/'ready.json').write_text('{}')
        with patch.object(d.subprocess,'run',return_value=subprocess.CompletedProcess([],25)):
            self.assertEqual(d.route(self.root)['mode'],'backup_only')
    def test_preflight_timeout_cannot_block_backup(self):
        self.enable();(self.root/'ready.json').write_text('{}')
        with patch.object(d.subprocess,'run',side_effect=subprocess.TimeoutExpired([],15)):
            self.assertEqual(d.route(self.root)['mode'],'backup_only')

if __name__=='__main__':unittest.main(verbosity=2)
