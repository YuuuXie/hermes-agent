#!/usr/bin/env python3
"""Cheap daily entrypoint: backup only, or backup + receipt-only adoption.
Never starts the old merge/test updater. --check is read-only and launches nothing.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path('/home/yuxie1/.hermes/maintenance/prepared-updates')
PYTHON='/home/yuxie1/.hermes/hermes-agent/venv/bin/python'
BACKUP='/home/yuxie1/.hermes/scripts/daily-backup-prepared.py'
MAINTENANCE='/home/yuxie1/.hermes/scripts/maintenance.py'

def route(root=ROOT):
    fallback={'mode':'backup_only','argv':[PYTHON,BACKUP]}
    try:
        setting=json.loads((root/'adoption.json').read_text())
    except (OSError,ValueError):
        return {**fallback,'reason':'adoption configuration unavailable'}
    if not isinstance(setting,dict) or setting.get('enabled') is not True:
        return {**fallback,'reason':'adoption disabled'}
    if not (root/'ready.json').is_file():
        return {**fallback,'reason':'no READY candidate'}
    config=root/'maintenance-config.json'
    try:
        check=subprocess.run([PYTHON,MAINTENANCE,'check-config','--config',str(config)],
                             capture_output=True,text=True,timeout=15)
    except (OSError,subprocess.TimeoutExpired):
        return {**fallback,'reason':'adoption preflight unavailable; backup remains independent'}
    if check.returncode!=0:
        return {**fallback,'reason':'adoption preflight refused; backup remains independent'}
    return {'mode':'backup_then_adopt','argv':[PYTHON,MAINTENANCE,'launch','--config',str(config)],
            'reason':'verified backup must precede receipt-only adoption in external controller'}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true')
    args=parser.parse_args();selected=route()
    if args.check:
        print(json.dumps(selected,indent=2));return 0
    if selected['mode']=='backup_only':
        print('更新采用：'+selected['reason'],flush=True)
        os.execv(PYTHON,selected['argv'])
    try:
        result=subprocess.run(selected['argv'],timeout=30)
    except (OSError,subprocess.TimeoutExpired):
        result=subprocess.CompletedProcess(selected['argv'],1)
    if result.returncode==0:
        print('已提交后台备份/采用任务；这只是启动成功，最终结果以维护回执与完成通知为准。')
        return 0
    if result.returncode==24:
        print('已有维护任务在执行，未重复启动。');return 24
    print('采用入口未确认成功，尝试纯备份路径（受同一维护锁保护）。',flush=True)
    os.execv(PYTHON,[PYTHON,BACKUP])

if __name__=='__main__':sys.exit(main())
