"""Task Scheduler가 시작하는 무콘솔 감독 프로세스. 재시작/모델 재시도 없음."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback


def identity():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetConsoleWindow.restype=wintypes.HWND
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]
    in_job=wintypes.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(),None,ctypes.byref(in_job)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {'pid':os.getpid(),'ppid':os.getppid(),'executable':sys.executable,
            'base_executable':getattr(sys,'_base_executable',None),
            'console_hwnd':int(kernel.GetConsoleWindow() or 0),'in_job':bool(in_job.value)}


def append(path,row):
    with path.open('a',encoding='utf-8') as f:
        f.write(json.dumps(dict(at=time.time(),**row),ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())


class ProcessTree:
    """관찰한 자손의 핸들을 유지하여 종료 후에도 실제 종료 코드를 기록한다."""
    def __init__(self,root,events):
        self.root,self.events=root,events
        self.known={root};self.handles={};self.exited={}
        self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        self.k.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD]
        self.k.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
        self.k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        self.k.OpenProcess.restype=wintypes.HANDLE
        self.k.CloseHandle.argtypes=[wintypes.HANDLE]
        self.k.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    def poll(self):
        class Entry(ctypes.Structure):
            _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),('pid',wintypes.DWORD),
                      ('heap',ctypes.c_size_t),('module',wintypes.DWORD),('threads',wintypes.DWORD),
                      ('ppid',wintypes.DWORD),('priority',wintypes.LONG),('flags',wintypes.DWORD),('exe',wintypes.WCHAR*260)]
        self.k.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
        self.k.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
        snapshot=self.k.CreateToolhelp32Snapshot(2,0)
        if snapshot==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
        entries=[];entry=Entry();entry.dwSize=ctypes.sizeof(entry)
        try:
            more=self.k.Process32FirstW(snapshot,ctypes.byref(entry))
            while more:
                entries.append((entry.pid,entry.ppid,entry.exe))
                more=self.k.Process32NextW(snapshot,ctypes.byref(entry))
        finally:self.k.CloseHandle(snapshot)
        changed=True
        while changed:
            changed=False
            for pid,ppid,exe in entries:
                if ppid in self.known and pid not in self.known:
                    self.known.add(pid);changed=True
        for pid,ppid,exe in entries:
            if pid in self.known and pid not in self.handles:
                handle=self.k.OpenProcess(0x1000|0x100000,False,pid)
                if handle:
                    self.handles[pid]=handle
                    append(self.events,{'event':'process_observed','pid':pid,'ppid':ppid,'image':exe})
        for pid,handle in self.handles.items():
            if pid in self.exited:continue
            code=wintypes.DWORD()
            if self.k.GetExitCodeProcess(handle,ctypes.byref(code)) and code.value!=259:
                self.exited[pid]=code.value
                append(self.events,{'event':'process_exited','pid':pid,'exit_code':code.value,'exit_hex':f'0x{code.value:08x}'})
    def close(self):
        for handle in self.handles.values():self.k.CloseHandle(handle)


def supervise(request):
    request=Path(request).resolve();data=json.loads(request.read_text('utf-8'))
    out=Path(data['output']).resolve();out.mkdir(exist_ok=True)
    # 중복 수동 시작·Task Scheduler 재실행도 한 번의 claim 뒤에서는 실행하지 않는다.
    with (out/'supervisor-claim.json').open('x',encoding='utf-8') as f:
        json.dump(identity(),f,ensure_ascii=False,indent=2)
    events=out/'supervisor.jsonl';append(events,{'event':'supervisor_started','identity':identity()})
    code=None;tree=None;remaining=[]
    try:
        command=data['command']
        if not command or Path(command[0]).name.lower()!='pythonw.exe':raise ValueError('무콘솔 Python 실행기 필요')
        env=dict(os.environ, PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        # 인증 값은 요청 JSON이나 로그에 넣지 않는다. 지정된 로컬 파일에서만 읽는다.
        if data.get('environment_file'):
            extra=json.loads(Path(data['environment_file']).read_text('utf-8'))
            if not isinstance(extra,dict) or any(not isinstance(v,str) for v in extra.values()):raise ValueError('환경 파일 형식')
            env.update(extra)
        missing=[key for key in data.get('required_environment',[]) if not env.get(key)]
        if missing:raise ValueError('필수 실행 환경 누락: '+', '.join(missing))
        runtime=Path(__file__).with_name('windows_runtime')
        env['PIXLATE_PROCESS_LOG_ROOT']=str(out/'process-logs')
        env['PYTHONPATH']=str(runtime)+(os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        with (out/'console.stdout.log').open('xb') as stdout,(out/'console.stderr.log').open('xb') as stderr:
            proc=subprocess.Popen(command,cwd=data['cwd'],env=env,stdin=subprocess.DEVNULL,
                                  stdout=stdout,stderr=stderr,close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW)
            append(events,{'event':'worker_started','pid':proc.pid,'parent_pid':os.getpid()})
            tree=ProcessTree(proc.pid,events)
            while proc.poll() is None:
                tree.poll()
                append(events,{'event':'worker_alive','pid':proc.pid});time.sleep(1)
            code=proc.returncode
            tree.poll();remaining=sorted(set(tree.handles)-set(tree.exited))
            append(events,{'event':'worker_exited','pid':proc.pid,'exit_code':code,'exit_hex':f'0x{code & 0xffffffff:08x}'})
    except BaseException:
        append(events,{'event':'supervisor_error','traceback':traceback.format_exc()})
        raise
    finally:
        if tree:tree.close()
        with (out/'supervisor-complete.json').open('x',encoding='utf-8') as f:
            json.dump({'worker_exit_code':code,'worker_result_confirmed':code is not None,
                       'success':code==0 and not remaining,'observed_live_descendants':remaining,
                       'automatic_restart':False,'at':time.time()},f,indent=2)
    return code


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('request');args=parser.parse_args()
    sys.exit(supervise(args.request))
