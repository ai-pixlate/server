"""보존 색인에 따라 별도 빈 디렉터리에 증거 사본을 복원한다. 채점/네트워크 호출 없음."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import shutil
import subprocess
import re


def restore(index_path, repository, destination):
    index = json.loads(index_path.read_text(encoding='utf-8'))
    repository = repository.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise ValueError('새 출력 폴더를 지정하세요. 기존 파일을 덮어쓰지 않습니다.')
    for scope in index['scope']:
        if destination.is_relative_to((repository / scope).resolve()):
            raise ValueError('보존 자료 밖의 출력 폴더를 지정하세요.')
    planned = []
    for item in index['files']:
        for key in (item['destination'], item['source']['path']):
            path = PurePosixPath(key)
            if path.is_absolute() or '..' in path.parts or '\\' in key or ':' in key:
                raise ValueError('안전하지 않은 상대 경로')
        source = (repository / item['source']['path']).resolve()
        if not source.is_relative_to(repository):
            raise ValueError('저장소 밖 증거 참조')
        payload = source.read_bytes() if source.is_file() else b''
        pinned_payload = None
        commit = item['source']['commit']
        if hashlib.sha256(payload).hexdigest() != item['sha256'] and commit != 'PR_HEAD':
            if not re.fullmatch(r'[0-9a-f]{40}', commit):
                raise ValueError('잘못된 고정 커밋')
            payload = subprocess.check_output(['git', 'show', commit + ':' + item['source']['path']], cwd=repository)
            pinned_payload = payload
        if len(payload) != item['bytes'] or hashlib.sha256(payload).hexdigest() != item['sha256']:
            raise ValueError('증거 해시 불일치: ' + item['source']['path'])
        planned.append((source, destination / item['destination'], item['sha256'], pinned_payload))
    destination.mkdir(parents=True)
    for source, target, expected, pinned_payload in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        if pinned_payload is None:
            shutil.copyfile(source, target)
        else:
            target.write_bytes(pinned_payload)
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError('복원 파일 해시 불일치: ' + str(target))
    return len(planned)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    count = restore(Path(__file__).with_name('preservation-index.json'), args.repository, args.output)
    print(f'{count}개 증거 파일 복원·SHA-256 확인 완료. 채점은 수행하지 않았습니다.')
