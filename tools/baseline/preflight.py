"""파일·설치 메타데이터만 확인. 모델 import·DB 연결·자격증명 출력 없음."""
import importlib.metadata
import json
import platform
import subprocess
from pathlib import Path

from .core import digest, inspect_inputs, read


def inspect(input_root, product_root=None, expected_commit=None, asset_manifest=None):
    inputs, issues = inspect_inputs({'input_root': str(input_root)})
    manifest_path = Path(input_root) / 'manifest.json'
    if manifest_path.is_file():
        manifest = read(manifest_path)
        known = {x['source_id']: x['sha256'] for x in manifest['files']}
        if len(known) != 34 or len(manifest['files']) != 34 or any(known.get(x['source_id']) != x['sha256'] for x in inputs):
            issues.append('평가셋 manifest 해시/목록 불일치')
    else:
        issues.append('평가셋 manifest 없음')
    packages = {}
    for name in ('Pillow', 'SQLAlchemy', 'psycopg', 'alembic', 'google-genai', 'paddlepaddle', 'paddleocr', 'torch'):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
            issues.append(name + ': 미설치')
    commit = None
    if product_root:
        try:
            commit = subprocess.check_output(['git', '-C', str(product_root), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
            if not expected_commit or commit != expected_commit:
                issues.append('제품 checkout과 배포 확인 커밋 대조 필요')
            dirty = subprocess.check_output(['git', '-C', str(product_root), 'status', '--porcelain', '--', 'app', 'pipeline', 'migrations'], text=True)
            if dirty.strip():
                issues.append('제품 checkout 미커밋 변경')
        except subprocess.CalledProcessError:
            issues.append('제품 checkout 확인 불가')
    else:
        issues.append('제품 checkout 미지정')
    assets = []
    if asset_manifest:
        for a in read(asset_manifest)['model_assets']:
            path = Path(a['path'])
            ok = path.is_file() and digest(path) == a['sha256']
            assets.append({'file': path.name, 'sha256': a['sha256'], 'matched': ok})
            if not ok:
                issues.append('모델 자산 불일치: ' + path.name)
    else:
        issues.append('모델 자산 목록 미지정')
    return {'platform': platform.platform(), 'python': platform.python_version(), 'packages': packages,
            'input_count': len(inputs), 'input_hashes': {i['source_id']: i['sha256'] for i in inputs},
            'product_commit': commit, 'model_assets': assets, 'issues': issues,
            'database_check': '별도 localhost 읽기 전용 점검 필요',
            'model_loaded': False, 'executed': False, 'approval_pending': True,
            'formal_ready': False}


def dependency_comparison(constraints):
    """운영 API 이미지의 고정값과 로컬 설치 메타데이터 대조. 설치·업그레이드 없음."""
    import re
    installed = {re.sub(r"[-_.]+", "-", d.metadata['Name']).lower(): d.version
                 for d in importlib.metadata.distributions() if d.metadata['Name']}
    result=[]
    for line in Path(constraints).read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#') or '==' not in line: continue
        name,expected=line.split('==',1)
        actual=installed.get(re.sub(r"[-_.]+", "-",name).lower())
        result.append({'package':name,'production':expected,'local':actual,'same':actual==expected})
    return {'constraints_sha256':digest(constraints),'packages':result,
            'note':'API 이미지 기준이며 학교 OCR/GPU 환경 고정값을 뜻하지 않음. 버전 차이만으로 호환 또는 비호환 단정 금지'}
