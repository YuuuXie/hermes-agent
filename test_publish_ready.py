"""READY publication tests with real temporary Git and fixture receipts."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_ffadopt as fixtures
import ffadopt
import publish_ready

class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AdopterTest(methodName='runTest');self.f.setUp()
        self.f.ready_path.unlink()
    def tearDown(self):self.f.tearDown()
    def test_eligible_check_does_not_publish_or_fetch(self):
        with patch.object(ffadopt,'fetch_pinned',side_effect=AssertionError('check-only must not fetch')):
            result=publish_ready.publish(self.f.cfg_path,self.f.receipt,True)
        self.assertEqual(result['status'],'receipt_eligible')
        self.assertFalse(self.f.ready_path.exists())
    def test_publish_reads_real_remote_and_is_adoptable(self):
        result=publish_ready.publish(self.f.cfg_path,self.f.receipt)
        self.assertTrue(result['ready_published'])
        self.assertEqual(self.f.apply(),11)
        self.assertEqual(self.f.head(),self.f.g.cand_sha)
    def test_failed_receipt_cannot_publish(self):
        d=json.loads(self.f.receipt.read_text());d['status']='aborted';self.f.receipt.write_text(json.dumps(d))
        with self.assertRaises(ffadopt.Refused):publish_ready.publish(self.f.cfg_path,self.f.receipt)
        self.assertFalse(self.f.ready_path.exists())
    def test_remote_movement_does_not_replace_last_ready(self):
        publish_ready.publish(self.f.cfg_path,self.f.receipt)
        previous=self.f.ready_path.read_bytes()
        fixtures.run_git(self.f.g.remote,'update-ref','refs/heads/main',self.f.g.base_sha)
        with self.assertRaises(ffadopt.Refused):publish_ready.publish(self.f.cfg_path,self.f.receipt)
        self.assertEqual(self.f.ready_path.read_bytes(),previous)

if __name__=='__main__':unittest.main(verbosity=2)
