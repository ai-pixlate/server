"""명시적으로 지정된 GS03 프로세스 트리의 로그만 연결한다. 이벤트 무시 없음."""
import atexit
import ctypes
import json
import msvcrt
import os
from pathlib import Path
import sys
import time

_root=os.environ.get('PIXLATE_PROCESS_LOG_ROOT')
if _root:
    _out=Path(_root)
    _out.mkdir(exist_ok=True)
    _pid=os.getpid()
    _kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    _kernel.GetConsoleWindow.restype=ctypes.c_void_p
    _kernel.SetStdHandle.argtypes=[ctypes.c_ulong,ctypes.c_void_p]
    for _name,_fd,_handle in [('stdout',1,-11),('stderr',2,-12)]:
        _stream=open(_out/f'process-{_pid}.{_name}.log','a',encoding='utf-8',buffering=1)
        os.dup2(_stream.fileno(),_fd)
        _kernel.SetStdHandle(_handle & 0xffffffff,msvcrt.get_osfhandle(_fd))
        setattr(sys,_name,_stream)
        setattr(sys,'__'+_name+'__',_stream)
    def _event(event):
        with (_out/f'process-{_pid}.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps({'event':event,'pid':_pid,'ppid':os.getppid(),'at':time.time(),
                               'executable':sys.executable,'console_hwnd':int(_kernel.GetConsoleWindow() or 0)})+'\n')
    _event('python_started')
    atexit.register(_event,'python_atexit')
