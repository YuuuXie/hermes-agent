import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT=Path('/home/yuxie1/.hermes/scripts/daily-backup-prepared.py')

class BackupSplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('backup_prepared',SCRIPT)
        cls.m=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def test_missing_candidate_never_updates(self):
        with tempfile.TemporaryDirectory() as d:
            result=self.m.prepared_status(Path(d))
        self.assertEqual(result['adoption'],'no_ready_candidate')

    def test_corrupt_status_is_not_ready(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'ready.json').write_text('{broken')
            result=self.m.prepared_status(Path(d))
        self.assertEqual(result['adoption'],'blocked_invalid_ready_record')

    def test_even_ready_cannot_invoke_missing_installer(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'ready.json').write_text(json.dumps({'status':'ready','candidate_sha':'a'*40}))
            with patch.object(self.m.subprocess,'Popen',side_effect=AssertionError('must not deploy')):
                result=self.m.prepared_status(Path(d))
        self.assertEqual(result['adoption'],'blocked_receipt_consumer_not_installed')

    def test_marker_and_exit_both_required(self):
        log='BACKUP_VERIFIED https://github.com/YuuuXie/chilicraft/releases/tag/test bytes=123\n'
        self.assertEqual(self.m.backup_evidence(0,log)['bytes'],123)
        self.assertIsNone(self.m.backup_evidence(1,log))
        self.assertIsNone(self.m.backup_evidence(0,'Backup complete'))
        self.assertIsNone(self.m.backup_evidence(0,'BACKUP_VERIFIED https://wrong.example/a bytes=123'))

    def test_real_child_exit_and_log(self):
        with tempfile.TemporaryDirectory() as d:
            log=Path(d,'log')
            rc=self.m.run_backup(['/bin/sh','-c','printf test; exit 7'],log,2)
            self.assertEqual(rc,7)
            self.assertEqual(log.read_text(),'test')

    def test_timeout_is_failure(self):
        with tempfile.TemporaryDirectory() as d:
            rc=self.m.run_backup(['/bin/sleep','10'],Path(d,'log'),0.1)
        self.assertEqual(rc,124)

    def test_main_uses_real_fixture_script_and_writes_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d); (home/'scripts').mkdir(); state=home/'maintenance/prepared-updates'
            (home/'scripts/backup-to-github.sh').write_text('printf "BACKUP_VERIFIED https://github.com/YuuuXie/chilicraft/releases/tag/fixture bytes=123\\n"\n')
            with patch.object(self.m,'HOME',home),patch.object(self.m,'STATE',state),patch.object(self.m.sys,'argv',['daily-backup-prepared.py']):
                self.assertEqual(self.m.main(),0)
            receipt=json.loads((state/'latest-backup.json').read_text())
            self.assertEqual(receipt['backup'],'verified')
            self.assertEqual(receipt['adoption'],'no_ready_candidate')
            self.assertFalse(receipt['production_changed'])

    def test_main_failure_does_not_inspect_or_adopt(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d); (home/'scripts').mkdir(); state=home/'maintenance/prepared-updates'
            (home/'scripts/backup-to-github.sh').write_text('exit 9\n')
            with patch.object(self.m,'HOME',home),patch.object(self.m,'STATE',state),patch.object(self.m.sys,'argv',['daily-backup-prepared.py']),patch.object(self.m,'prepared_status',side_effect=AssertionError('must stop after backup failure')):
                self.assertEqual(self.m.main(),20)
            self.assertEqual(json.loads((state/'latest-backup.json').read_text())['adoption'],'not_checked_backup_failed')

    def test_bad_attempt_record_does_not_break_backup_inspection(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'latest-attempt.json').write_text('[]')
            result=self.m.prepared_status(Path(d))
        self.assertEqual(result['latest_preparation'],'invalid_attempt_record')
        self.assertEqual(result['adoption'],'no_ready_candidate')

if __name__=='__main__': unittest.main()
