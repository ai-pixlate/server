"""Windows spawn에 안전한 GS-03 실행 진입점. import 시 실행·환경 변경 없음."""
import argparse
import multiprocessing
import os
from pathlib import Path


ENV_KEYS = ('PADDLE_PDX_CACHE_HOME', 'PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK',
            'HF_HUB_OFFLINE', 'MODELSCOPE_OFFLINE', 'PYTHONIOENCODING',
            'PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE', 'AWS_EC2_METADATA_DISABLED')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--url-file', required=True, type=Path)
    parser.add_argument('--execute-models', action='store_true')
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
    preflight(out)  # 이미 시작한 회차는 실행 기록 생성 전에 거부
    require_approval(out)
    (out / 'tmp').mkdir(exist_ok=True)
    os.environ['TEMP'] = os.environ['TMP'] = str(out / 'tmp')
    write_new(out / 'launch.json', {
        'at': now(), 'entrypoint': 'tools.baseline.gs03_launch',
        'environment': {k: os.environ[k] for k in ENV_KEYS + ('TEMP', 'TMP')},
        'secret_values_recorded': False,
    })
    from tools.baseline.gs03_runner import _run_prepared
    return _run_prepared(out, True)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
