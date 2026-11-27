import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid
from tools.baseline.gs03_scheduled import make_request, task_action, pythonw


@unittest.skipUnless(os.name=='nt' and os.environ.get('PIXLATE_WINDOWS_LIVE_TESTS')=='1',
                     '명시적 실제 Windows 비과금 통합 테스트')
class WindowsServiceTest(unittest.TestCase):
    def wait_file(self,path,timeout=60):
        end=time.monotonic()+timeout
        while not path.exists() and time.monotonic()<end:time.sleep(.1)
        self.assertTrue(path.exists(),str(path))
        # 작성 완료 뒤 JSON을 읽는다.
        for _ in range(20):
            try:return json.loads(path.read_text('utf-8'))
            except json.JSONDecodeError:time.sleep(.1)
        self.fail('incomplete JSON: '+str(path))

    def run_case(self,mode):
        root=Path(os.environ['PIXLATE_WINDOWS_PROBE_ROOT']).resolve()
        out=root/(mode+'-'+uuid.uuid4().hex[:8]);out.mkdir()
        repo=Path(__file__).resolve().parents[1]
        request=make_request(out,[pythonw(sys.executable),'-u','-B','-m','tools.baseline.windows_probe','payload',str(out),'--native'])
        startup=subprocess.STARTUPINFO();startup.dwFlags=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=0
        with (out/'controller.stdout').open('wb') as stdout,(out/'controller.stderr').open('wb') as stderr:
            parent=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-B','-m','tools.baseline.windows_probe',
                      'controller',str(out),'--request',str(request)],cwd=repo,stdout=stdout,stderr=stderr,
                      startupinfo=startup,creationflags=subprocess.CREATE_NEW_CONSOLE)
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.restype=wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes=[wintypes.HANDLE,wintypes.UINT]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        job=kernel.CreateJobObjectW(None,None)
        self.assertTrue(job)
        try:
            self.assertTrue(kernel.AssignProcessToJobObject(job,int(parent._handle)),ctypes.get_last_error())
            controller=self.wait_file(out/'controller.json')
            (out/'controller-go').touch()
            self.wait_file(out/'controller-submitted.json')
            worker=self.wait_file(out/'probe-worker-started.json')
            child=self.wait_file(out/'probe-child-started.json',timeout=90)
            self.assertEqual(worker['console_hwnd'],0)
            self.assertEqual(child['console_hwnd'],0)
            self.assertNotEqual(worker['ppid'],parent.pid)
            self.assertNotEqual(child['pid'],worker['pid'])
            if mode=='console_close':
                self.assertNotEqual(controller['console_hwnd'],0)
                user=ctypes.WinDLL('user32',use_last_error=True)
                user.PostMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
                self.assertTrue(user.PostMessageW(controller['console_hwnd'],0x10,0,0))
            elif mode=='job_terminate':
                self.assertTrue(kernel.TerminateJobObject(job,91))
            else:(out/'controller-normal-exit').touch()
            parent.wait(timeout=15);parent_exit=time.time()
            done=self.wait_file(out/'supervisor-complete.json')
            child_done=self.wait_file(out/'probe-child-complete.json')
            worker_done=self.wait_file(out/'probe-worker-complete.json')
            self.assertGreater(child_done['at'],parent_exit)
            self.assertEqual(worker_done['child_exit_code'],0)
            self.assertEqual(done['worker_exit_code'],0)
            self.assertEqual(done['observed_live_descendants'],[])
            events=[json.loads(s) for s in (out/'supervisor.jsonl').read_text('utf-8').splitlines()]
            exits={e['pid']:e['exit_code'] for e in events if e['event']=='process_exited'}
            self.assertEqual(exits[worker['pid']],0)
            self.assertEqual(exits[child['pid']],0)
            self.assertIn('probe child complete',(out/'process-logs'/f"process-{child['pid']}.stdout.log").read_text('utf-8'))
            self.assertIn('probe worker complete',(out/'process-logs'/f"process-{worker['pid']}.stdout.log").read_text('utf-8'))
            status=json.loads(task_action(request,'status'))
            until=time.monotonic()+10
            while status['state']==4 and time.monotonic()<until:
                time.sleep(.2);status=json.loads(task_action(request,'status'))
            self.assertEqual(status['last_result'],0)
            proof={'mode':mode,'controller':controller,'parent_exit_code':parent.returncode,'parent_exit_at':parent_exit,
                   'worker':worker,'child':child,'child_completed_at':child_done['at'],
                   'supervisor':done,'scheduler_status':status,'model_calls':0}
            (out/'verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),'utf-8')
            task_action(request,'remove')
            print('VERIFIED',out,flush=True)
        finally:
            if parent.poll() is None:kernel.TerminateJobObject(job,92);parent.wait(timeout=10)
            kernel.CloseHandle(job)

    def test_parent_console_close(self):self.run_case('console_close')
    def test_parent_job_termination(self):self.run_case('job_terminate')
    def test_parent_normal_exit(self):self.run_case('normal_exit')

class ScheduledRequestTest(unittest.TestCase):
    def test_claim_and_started_guards(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch('tools.baseline.gs03_scheduled.pythonw',return_value='pythonw.exe'):
            out=Path(tmp)
            path=make_request(out,['pythonw.exe'],required_environment=('GEMINI_API_KEY',))
            self.assertEqual(json.loads(path.read_text('utf-8'))['required_environment'],['GEMINI_API_KEY'])
            with self.assertRaises(FileExistsError):make_request(out,['pythonw.exe'])
            (out/'started.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'재실행 금지'):make_request(out,['pythonw.exe'])
