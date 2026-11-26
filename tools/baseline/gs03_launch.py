"""Windows spawn에 안전한 GS-03 실행 진입점. import 시 실행·환경 변경 없음."""
import argparse
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path


ENV_KEYS = ('PADDLE_PDX_CACHE_HOME', 'PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK',
            'HF_HUB_OFFLINE', 'MODELSCOPE_OFFLINE', 'PYTHONIOENCODING',
            'PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE', 'AWS_EC2_METADATA_DISABLED')


def detached_process(command, out):
    """stdin 없이 독립 프로세스로 실행하고 로그를 파일에 보존한다."""
    if os.name != 'nt':raise ValueError('검증 대상은 Windows')
    with (out/'console.stdout.log').open('xb') as stdout, (out/'console.stderr.log').open('xb') as stderr:
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            cwd=Path(__file__).resolve().parents[2],close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--url-file', required=True, type=Path)
    parser.add_argument('--execute-models', action='store_true')
    parser.add_argument('--detach', action='store_true')
    args = parser.parse_args(argv)
    if not args.execute_models:
        parser.error('실제 실행은 별도 승인 후 --execute-models 필요')
    from tools.baseline.core import read, write_new, now
    from tools.baseline.gs03_supplement import preflight, require_approval
    out = args.output.resolve()
    manifest = read(out / 'manifest.json')
    previous = read(Path(manifest['supplement']['baseline_root']) / 'launch.json')
    for key in ENV_KEYS:
        os.environ[key] = previous['environment'][key]
    os.environ['BASELINE_DATABASE_URL'] = args.url_file.read_text(encoding='utf-8').strip()
    recovery = manifest.get('recovery') is not None
    if recovery:
        from tools.baseline.gs03_recovery import preflight as recovery_preflight, require_execution
        recovery_preflight(out, args.url_file)
        require_execution(out)
    else:
        preflight(out)  # 이미 시작한 회차는 실행 기록 생성 전에 거부
        require_approval(out)
    if args.detach:
        if not recovery:raise ValueError('무대기 복제 실험만 독립 실행 가능')
        write_new(out/'detached-start.json',{'at':now(),'entrypoint':'tools.baseline.gs03_launch','human_reviewed':False})
        child=detached_process([sys.executable,'-u','-B','-m','tools.baseline.gs03_launch',str(out),
            '--url-file',str(args.url_file.resolve()),'--execute-models'],out)
        write_new(out/'detached-process.json',{'pid':child.pid,'at':now()})
        print('독립 프로세스 시작:',child.pid)
        return
    (out / 'tmp').mkdir(exist_ok=True)
    os.environ['TEMP'] = os.environ['TMP'] = str(out / 'tmp')
    write_new(out / 'launch.json', {
        'at': now(), 'entrypoint': 'tools.baseline.gs03_launch',
        'environment': {k: os.environ[k] for k in ENV_KEYS + ('TEMP', 'TMP')},
        'secret_values_recorded': False,
    })
    from tools.baseline.gs03_runner import _run_prepared
    if recovery:
        return _run_prepared(out, True, recovery_url_file=args.url_file)
    return _run_prepared(out, True)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
