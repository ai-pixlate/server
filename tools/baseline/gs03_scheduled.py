"""Codex 터미널 대신 Windows Task Scheduler의 일회 실행으로 감독자를 시작한다."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid


def pythonw(executable):
    path=Path(executable).with_name('pythonw.exe')
    if not path.is_file():raise ValueError('pythonw.exe 없음')
    return str(path.resolve())


def make_request(out,command,*,environment_file=None,required_environment=()):
    out=Path(out).resolve();out.mkdir(exist_ok=True)
    if (out/'started.json').exists() or (out/'generation-claims').exists():raise ValueError('시작된 회차 재실행 금지')
    request=out/'scheduled-request.json'
    data={'task_name':'Pixlate-GS03-'+uuid.uuid4().hex,'output':str(out),
          'cwd':str(Path(__file__).resolve().parents[2]),'command':command,
          'supervisor_executable':pythonw(getattr(sys,'_base_executable',sys.executable)),
          'supervisor_arguments':subprocess.list2cmdline(['-B','-m','tools.baseline.windows_supervisor',str(request)]),
          'environment_file':str(Path(environment_file).resolve()) if environment_file else None,
          'required_environment':list(required_environment)}
    with request.open('x',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2)
    return request


def task_action(request,action='start'):
    script=Path(__file__).with_name('windows_task.ps1')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden',
            '-File',str(script),'-Request',str(request),'-Action',action],capture_output=True,text=True,
            encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise RuntimeError('작업 스케줄러 실패: '+result.stderr.strip())
    return result.stdout.strip()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path);parser.add_argument('--url-file',required=True,type=Path)
    parser.add_argument('--environment-file',type=Path)
    parser.add_argument('--execute-models',action='store_true')
    args=parser.parse_args()
    if not args.execute_models:parser.error('실제 실행은 별도 승인 후 --execute-models 필요')
    from .core import read,write_new,now
    m=read(args.output/'manifest.json')
    if m['recovery']['mode']=='gs03_n4_job06_clone':
        from .gs03_n4_execution import preflight,require_execution
    elif m['recovery']['mode']=='gs03_n4_restart_clone':
        from .gs03_n4_restart import preflight,require_execution
    else:
        from .gs03_recovery import preflight,require_execution
    preflight(args.output,args.url_file);require_execution(args.output)
    command=[pythonw(sys.executable),'-u','-B','-m','tools.baseline.gs03_launch',str(args.output.resolve()),
             '--url-file',str(args.url_file.resolve()),'--execute-models']
    request=make_request(args.output,command,environment_file=args.environment_file,required_environment=('GEMINI_API_KEY',))
    result=task_action(request)
    write_new(args.output/'scheduled-start.json',{'at':now(),'result':json.loads(result)})
    print(result)


if __name__=='__main__':main()
