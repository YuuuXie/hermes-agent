#!/usr/bin/env python3
"""Publish READY only from a successful, config-bound verifier receipt.
Does not commit, push, install, repair, or run tests. The preparation job pushes
the exact verified commit to the configured ready ref before final publication.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import sys
import ffadopt


def publish(config_path, receipt_path, check_only=False):
    cfg=ffadopt.load_json(Path(config_path),'config')
    ffadopt.check_config(cfg)
    receipt_path=Path(receipt_path).resolve()
    raw=receipt_path.read_bytes()
    receipt=json.loads(raw)
    ready={'schema':2,'status':'ready','candidate_sha':receipt.get('candidate_sha'),
           'candidate_tree':receipt.get('verified_tree'),'base_sha':receipt.get('live_head_before'),
           'receipt_path':str(receipt_path),'receipt_sha256':hashlib.sha256(raw).hexdigest()}
    for key in ('verifier_path','verifier_sha256','manifest_path','manifest_sha256','remote_url','remote_ref'):
        ready[key]=cfg[key]
    ffadopt.check_ready(cfg,ready)
    ffadopt.check_receipt(cfg,ready)
    if check_only:
        return {'status':'receipt_eligible','candidate_sha':ready['candidate_sha'],'ready_published':False}
    scratch=Path(tempfile.mkdtemp(prefix='hermes-ready-publish-'))
    try:
        deadline=ffadopt.Deadline(float(cfg.get('apply_deadline_seconds',120)))
        ffadopt.fetch_pinned(cfg,ready,scratch,deadline)
        changed=ffadopt.changed_dependency_files(scratch,ready['base_sha'],ready['candidate_sha'],deadline)
        if changed:
            ffadopt.refuse('independent_dependency_manifest_change',changed=changed)
        # Receipt/content may have changed during network I/O; publication
        # must still bind the same bytes. No caller-provided success flag.
        ffadopt.check_ready(cfg,ready)
        ffadopt.check_receipt(cfg,ready)
        ffadopt.atomic_write_json(Path(cfg['ready_file']),ready)
        return {'status':'ready','candidate_sha':ready['candidate_sha'],'ready_file':cfg['ready_file'],'ready_published':True}
    finally:
        shutil.rmtree(scratch,ignore_errors=True)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--config',required=True)
    p.add_argument('--receipt',required=True)
    p.add_argument('--check-receipt',action='store_true')
    a=p.parse_args()
    try:
        print(json.dumps(publish(a.config,a.receipt,a.check_receipt),indent=2));return 0
    except (ffadopt.Refused,OSError,ValueError,TypeError,KeyError) as e:
        print(json.dumps({'status':'refused','ready_published':False,'reason':str(e)}));return 30

if __name__=='__main__':sys.exit(main())
