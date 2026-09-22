#!/usr/bin/env python3
"""Backup-only scheduled path plus read-only prepared-release inspection.

Never calls any updater. The daily dispatcher routes READY records to the
separate receipt-only controller; this pure-backup fallback only reports state.
READY metadata alone is not permission to improvise a git pull or restart.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

HOME=Path('/home/yuxie1/.hermes')
STATE=HOME/'maintenance/prepared-updates'
MARKER=re.compile(r'^BACKUP_VERIFIED\s+(https://github\.com/YuuuXie/chilicraft/releases/\S+)\s+bytes=(\d+)\s*$',re.M)

def atomic_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
            f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def prepared_status(root):
    ready=root/'ready.json'
    attempt=root/'latest-attempt.json'
    result={'adoption':'no_ready_candidate','production_changed':False}
    if attempt.exists():
        try:
            data=json.loads(attempt.read_text())
            if not isinstance(data,dict): raise ValueError('attempt must be an object')
            result['latest_preparation']=data.get('status','unknown')
        except (OSError,ValueError): result['latest_preparation']='invalid_attempt_record'
    if not ready.exists(): return result
    try:
        data=json.loads(ready.read_text())
        if not isinstance(data,dict) or data.get('status')!='ready' or not re.fullmatch(r'[0-9a-f]{40}',str(data.get('candidate_sha',''))):
            raise ValueError('invalid ready schema')
    except (OSError,ValueError):
        result['adoption']='blocked_invalid_ready_record'
        return result
    try:
        setting=json.loads((root/'adoption.json').read_text())
        enabled=isinstance(setting,dict) and setting.get('enabled') is True
    except (OSError,ValueError):
        enabled=False
    result.update(adoption=('ready_record_requires_external_controller_validation' if enabled
                            else 'blocked_receipt_consumer_not_installed'),
                  candidate_sha=data['candidate_sha'])
    return result

def backup_evidence(rc,text):
    match=MARKER.search(text)
    if rc!=0 or not match or int(match.group(2))<=0: return None
    return {'url':match.group(1),'bytes':int(match.group(2))}

def run_backup(argv,log,timeout):
    with log.open('w') as output:
        process=subprocess.Popen(argv,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGTERM)
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait()
            return 124
        finally:
            # The known backup program does not detach sessions. Clean its
            # process group even when the shell has already exited.
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--check',action='store_true',help='read-only status; no backup or deployment')
    args=ap.parse_args()
    if args.check:
        print(json.dumps(prepared_status(STATE),ensure_ascii=False,indent=2));return 0
    os.umask(0o077)
    STATE.mkdir(parents=True,exist_ok=True)
    lock=HOME/'maintenance/maintenance.lock'
    with lock.open('a+') as guard:
        try: fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('本次备份未执行：已有维护任务持锁；未启动重复任务。');return 24
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        log=STATE/(stamp+'.backup.log')
        receipt={'started_at':stamp,'backup':'running','production_changed':False,'log':str(log)}
        try:
            rc=run_backup(['/bin/bash',str(HOME/'scripts/backup-to-github.sh')],log,3000)
            proof=backup_evidence(rc,log.read_text(errors='replace'))
        except Exception as exc:
            rc=1;proof=None;receipt['error']=str(exc)
        receipt.update(backup='verified' if proof else 'failed',backup_exit_code=rc,backup_evidence=proof)
        if proof:
            receipt.update(prepared_status(STATE))
        else:
            receipt['adoption']='not_checked_backup_failed'
        receipt['finished_at']=datetime.now(timezone.utc).isoformat()
        atomic_json(STATE/(stamp+'.backup.json'),receipt)
        atomic_json(STATE/'latest-backup.json',receipt)
        if not proof:
            print('备份失败或缺少远端资产校验；未采用更新、未重启。日志：'+str(log));return 20
        print('备份已验证：'+proof['url'])
        if receipt['adoption']=='no_ready_candidate':
            print('更新：暂无已验收 READY 版本，保留当前运行版本；未现场合并或测试。')
        else:
            print('更新采用未执行：'+receipt['adoption']+'；未改生产、未重启。')
        print('回执：'+str(STATE/(stamp+'.backup.json')))
        return 0

if __name__=='__main__': sys.exit(main())
