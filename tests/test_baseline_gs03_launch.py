"""실제 spawn 하위 프로세스 회귀. 제품·DB·모델은 호출하지 않는다."""
import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class LaunchTest(unittest.TestCase):
    def test_started_run_stays_blocked(self):
        from tools.baseline import gs03_launch as launch
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ), patch('tools.baseline.gs03_supplement.validate', return_value=[]):
            out = Path(tmp)
            baseline = out / 'baseline'
            baseline.mkdir()
            (out / 'manifest.json').write_text(json.dumps({'supplement': {'baseline_root': str(baseline)}}))
            (baseline / 'launch.json').write_text(json.dumps({'environment': {k: 'test' for k in launch.ENV_KEYS}}))
            url = out / 'url.txt'
            url.write_text('not-a-real-connection')
            (out / 'started.json').write_bytes(b'preserve-original')
            with self.assertRaisesRegex(ValueError, '자동 재실행 금지'):
                launch.main([str(out), '--url-file', str(url), '--execute-models'])
            self.assertEqual((out / 'started.json').read_bytes(), b'preserve-original')
            self.assertFalse((out / 'launch.json').exists())
            self.assertFalse((out / 'tmp').exists())

    def test_spawn_does_not_reenter_launch(self):
        # Windows는 실제 Windows spawn, 다른 OS에서도 동일 spawn 문맥 사용.
        # fake runner가 spawn을 호출하는 동안 __main__은 실제 gs03_launch다.
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / 'harness.py'
            harness.write_text('''
import sys, os, json, runpy, multiprocessing
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from tools.baseline import gs03_launch as launch, gs03_supplement as s
from tools.baseline import gs03_runner as runner
root=Path(sys.argv[2]); baseline=root/'baseline'; baseline.mkdir()
(root/'manifest.json').write_text(json.dumps({'supplement':{'baseline_root':str(baseline)}}))
(baseline/'launch.json').write_text(json.dumps({'environment':{k:'test' for k in launch.ENV_KEYS}}))
url=root/'url.txt';url.write_text('test-secret-not-a-real-connection')
def fake_run(out, execute):
    assert execute
    child=multiprocessing.get_context('spawn').Process(target=os.getpid)
    child.start();child.join(20)
    if child.is_alive():child.terminate();child.join();raise AssertionError('spawn timeout')
    assert child.exitcode == 0, child.exitcode
    assert 'app' not in sys.modules and 'pipeline' not in sys.modules
if __name__=='__main__':
    mode=sys.argv[3]
    sys.argv=['gs03_launch',str(root),'--url-file',str(url),'--execute-models']
    with patch.object(s,'preflight') as pre, patch.object(s,'require_approval') as approve, patch.object(runner,'_run_prepared',fake_run):
        if mode=='file':runpy.run_path(launch.__file__,run_name='__main__')
        else:runpy.run_module('tools.baseline.gs03_launch',run_name='__main__',alter_sys=True)
        pre.assert_called_once();approve.assert_called_once()
    assert 'test-secret' not in (root/'launch.json').read_text()
''', encoding='utf-8')
            for mode in ('file', 'module'):
                with self.subTest(mode=mode):
                    out = Path(tmp) / mode
                    out.mkdir()
                    result = subprocess.run([sys.executable, '-I', '-B', str(harness), str(ROOT), str(out), mode],
                                            capture_output=True, text=True, timeout=40)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_child_import_has_no_side_effects(self):
        code = (f'import sys,runpy,os;sys.path.insert(0,{str(ROOT)!r});'
                'before=dict(os.environ);'
                "runpy.run_module('tools.baseline.gs03_launch',run_name='__mp_main__');"
                "assert before==dict(os.environ);assert 'tools.baseline.gs03_runner' not in sys.modules")
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
