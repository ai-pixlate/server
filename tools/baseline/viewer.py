"""Offline, read-only result viewer. Never imports a product adapter or evaluates quality."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

from PIL import Image

from .core import digest, read, write_new


class MissingFile(ValueError):
    pass


def relative_path(value):
    value = str(value).replace('\\', '/')
    p = PurePosixPath(value)
    if not value or p.is_absolute() or '..' in p.parts or ':' in value:
        raise ValueError('허용되지 않는 상대 경로: ' + value)
    return p.as_posix()


class Files:
    """Resolver boundary: local run, restored asset roots, hash-pinned local ZIPs."""
    def __init__(self, root, run_id, asset_roots=(), archives=()):
        self.roots = [Path(root).resolve(), *[Path(p).resolve() for p in asset_roots]]
        self.run_id = relative_path(run_id)
        self.archives = []
        self.hashes = {}
        self.observed = {}
        self.cache = {}
        try:
            for path, expected in archives:
                if digest(path) != expected:
                    raise ValueError('ZIP SHA-256 불일치: ' + str(path))
                z = zipfile.ZipFile(path)
                self.archives.append(z)
                names = set()
                for info in z.infolist():
                    relative_path(info.filename)
                    if info.filename in names or (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError('중복/심볼릭 링크 ZIP 항목: ' + info.filename)
                    names.add(info.filename)
        except BaseException:
            self.close()
            raise

    def close(self):
        for z in self.archives:
            z.close()

    def get(self, path, expected=None):
        path = relative_path(path)
        if expected and self.hashes.get(path) and expected != self.hashes[path]:
            raise ValueError('증거 간 기대 SHA-256 충돌: ' + path)
        expected = expected or self.hashes.get(path)
        key = (path, expected)
        if key in self.cache:
            return self.cache[key]
        raw = None
        origin = None
        for root in self.roots:
            p = root / path
            if not p.resolve().is_relative_to(root):
                raise ValueError('자료 경로가 입력 폴더 밖을 가리킵니다: ' + path)
            if p.is_file():
                if p.stat().st_size > 512 * 1024**2:
                    raise ValueError('뷰어 단일 파일 읽기 제한(512 MiB): ' + path)
                raw, origin = p.read_bytes(), str(p)
                break
        if raw is None:
            for z in self.archives:
                for name in (path, self.run_id + '/' + path):
                    try:
                        info = z.getinfo(name)
                    except KeyError:
                        continue
                    if info.file_size > 512 * 1024**2:
                        raise ValueError('ZIP 항목 읽기 제한(512 MiB): ' + name)
                    raw, origin = z.read(info), str(z.filename) + '::' + name
                    break
                if raw is not None:
                    break
        if raw is None:
            raise MissingFile('로컬/아카이브에 파일 없음: ' + path)
        actual = hashlib.sha256(raw).hexdigest()
        if expected and actual != expected:
            raise ValueError('SHA-256 불일치: ' + path)
        self.observed[path] = {'sha256': actual, 'expected_sha256': expected, 'origin': origin}
        self.cache[key] = raw
        return raw

    def json(self, path, expected=None):
        return json.loads(self.get(path, expected).decode('utf-8-sig'))


def generate(run, output, *, asset_roots=(), archives=()):
    root, out = Path(run).resolve(), Path(output).resolve()
    inputs = [root, *[Path(p).resolve() for p in asset_roots]]
    if any(out.is_relative_to(p) or p.is_relative_to(out) for p in inputs):
        raise ValueError('출력은 입력 회차/자료 폴더와 겹치지 않는 별도 경로여야 합니다')
    if out.exists():
        raise ValueError('기존 출력은 덮어쓰지 않습니다: ' + str(out))
    manifest = read(root / 'manifest.json')
    files = Files(root, manifest['evaluation_run_id'], asset_roots, archives)
    try:
        return _generate(manifest, files, out)
    finally:
        files.close()


def _generate(m, files, out):
    warnings, assets = [], {}
    try:
        index = files.json('github-file-index.json')['files']
        files.hashes.update({p: v['sha256'] for p, v in index.items()})
    except MissingFile:
        pass
    for item in [*m.get('artifacts', []), *m.get('files', [])]:
        if item.get('path') and item.get('sha256'):
            if item['path'] in files.hashes and files.hashes[item['path']] != item['sha256']:
                raise ValueError('색인/manifest 해시 충돌: ' + item['path'])
            files.hashes[item['path']] = item['sha256']
    # Validate manifest itself against the optional preservation index.
    files.get('manifest.json')

    def optional_json(path):
        try:
            return files.json(path)
        except MissingFile as e:
            warnings.append(str(e))
            return {}

    try:
        events = [json.loads(s) for s in files.get('execution.jsonl').decode('utf-8-sig').splitlines() if s.strip()]
    except MissingFile:
        events = []
        warnings.append('execution.jsonl 없음: 실행 상태를 확정하지 않습니다')
    refs = {}
    for event in events:
        for ref in event.get('evidence_refs', []):
            if ref.get('path') and ref.get('sha256'):
                if ref['path'] in files.hashes and files.hashes[ref['path']] != ref['sha256']:
                    raise ValueError('실행 증거/색인 해시 충돌: ' + ref['path'])
                files.hashes[ref['path']] = ref['sha256']
                refs.setdefault(event.get('case_id'), set()).add(ref['path'])
    objects = optional_json('object-index.json')

    def picture(meta):
        if not meta:
            return {'url': None, 'reason': '해당 산출물 기록 없음'}
        path = meta.get('path')
        if not path:
            return {'url': None, 'reason': meta.get('unavailable_reason') or '파일 경로 없음'}
        try:
            raw = files.get(path, meta.get('sha256'))
            with Image.open(io.BytesIO(raw)) as im:
                width, height = im.size
                ext = {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp', 'GIF': 'gif'}.get(im.format)
                if not ext:
                    raise ValueError('지원하지 않는 이미지 형식: ' + path)
                im.verify()
            if meta.get('width') is not None and (width, height) != (meta['width'], meta['height']):
                raise ValueError('이미지 크기/메타데이터 불일치: ' + path)
            h = hashlib.sha256(raw).hexdigest()
            dest = 'assets/' + h + '.' + ext
            assets[dest] = raw
            return {'url': dest, 'width': width, 'height': height, 'sha256': h,
                    'artifact_id': meta.get('artifact_id'), 'path': path}
        except MissingFile as e:
            warnings.append(str(e))
            return {'url': None, 'path': path, 'reason': str(e)}

    def object_picture(key):
        entry = objects.get(key)
        return picture({'path': 'objects/' + entry['sha256'], 'sha256': entry['sha256']} if entry else None)

    records = m.get('artifacts', [])
    by_id = {a['artifact_id']: a for a in records}
    if len(by_id) != len(records):
        raise ValueError('중복 artifact_id')
    cases = dict(m.get('case_inputs', {}))
    for a in records:
        cases.setdefault(a['case_id'], {})
    for event in events:
        if event.get('case_id'):
            cases.setdefault(event['case_id'], {})
    result = []
    for case, info in cases.items():
        # Current BE evidence adapter; file name is a case identifier, never a source-number guess.
        db = {}
        if '/' not in case and '\\' not in case and ':' not in case:
            try:
                db = files.json('evidence/' + case + '.json')
            except MissingFile:
                pass
        if not db:
            for path in sorted(refs.get(case, ())):
                if Path(path).suffix.lower() != '.json':
                    continue
                candidate = optional_json(path)
                if isinstance(candidate, dict) and 'source_image' in candidate and 'job' in candidate:
                    db = candidate
                    break
        job = (db.get('job') or [{}])[0]
        case_artifacts = [a for a in records if a['case_id'] == case]
        original_artifacts = [a for a in case_artifacts if a.get('kind') == 'original']
        keys = list(dict.fromkeys(s for a in case_artifacts for s in a.get('source_keys', [])))
        keys += [s for e in events if e.get('case_id') == case for s in e.get('source_keys', []) if s not in keys]
        keys = list(dict.fromkeys(keys))
        case_errors = [{'task_id': t.get('id'), 'stage': t.get('stage'), 'attempt': t.get('attempt_no'),
                        'code': t.get('error_code'), 'message': t.get('error_message')}
                       for t in db.get('job_async_task', []) if t.get('error_code')]
        sources = []
        for source in keys:
            own = [a for a in case_artifacts if source in a.get('source_keys', [])]
            originals = [a for a in original_artifacts if source in a['source_keys']]
            original = originals[0] if len(originals) == 1 else None
            if not original:
                warnings.append(source + ': 원본 산출물 연결이 없거나 모호함')
            matches = [s for s in db.get('source_image', []) if original and s.get('sha256') == original.get('sha256')]
            # Identical originals are not disambiguated by upload_order or parsed source names.
            unique_original = original and sum(a.get('sha256') == original.get('sha256') for a in original_artifacts) == 1
            dbsource = matches[0] if len(matches) == 1 and unique_original else None
            if db.get('source_image') and not dbsource:
                warnings.append(source + ': DB 원본 해시 연결이 모호하거나 없음; 섹션을 추측하지 않음')
            sections = sorted([s for s in db.get('section', []) if dbsource and s['source_image_id'] == dbsource['id']], key=lambda s: s['section_order'])
            previews = [a for a in own if a.get('checkpoint') == 'machine_snapshot']
            finals = [a for a in own if a.get('checkpoint') == 'service_final']
            section_rows, matched = [], set()
            for sec in sections:
                paired = [a for a in previews if str(a.get('target_id')) == str(sec['id'])]
                for a in paired:
                    if a.get('state_ref'):
                        ref = a['state_ref']; state = files.json(ref['path'], ref['sha256'])['state']
                        # N5 maps block IDs, not section IDs; use its sections for target identity.
                        found = [s for s in state.get('sections', []) if str(s.get('id')) == str(sec['id'])]
                        if not found or any(s.get('source_image_id') != dbsource['id'] for s in found):
                            raise ValueError('체크포인트/섹션 원본 연결 불일치: ' + a['artifact_id'])
                    matched.add(a['artifact_id'])
                section_rows.append({'id': str(sec['id']), 'order': sec['section_order'], 'offset': sec.get('top_offset'),
                    'height': sec.get('height'), 'bucket': sec.get('bucket'), 'reason': sec.get('exclusion_reason'),
                    'original': object_picture(sec.get('image_key')),
                    'previews': [picture(a) for a in paired],
                    'placements': [x for x in db.get('deliverable_section', []) if x.get('section_id') == sec['id']]})
            # Portable manifest-native link: a checkpoint explicitly names its parent section artifact.
            for a in previews:
                if a['artifact_id'] in matched:
                    continue
                parent = by_id.get(a.get('parent_artifact_id'))
                valid = parent and parent.get('case_id') == case and source in parent.get('source_keys', []) and parent.get('kind') != 'original'
                section_rows.append({'id': a['artifact_id'], 'order': None, 'offset': None, 'height': None,
                    'bucket': None, 'reason': None, 'original': picture(parent) if valid else {'url': None, 'reason': '원본 섹션 연결 증거 없음'},
                    'previews': [picture(a)], 'placements': []})
                if not valid:
                    warnings.append(source + ': 미리보기 ' + a['artifact_id'] + '의 원본 섹션 연결 증거 없음')
            own_events = [e for e in events if source in e.get('source_keys', []) and e.get('case_id') == case]
            errors = [{'record_id': e.get('record_id'), 'stage': e.get('stage'), 'codes': e.get('error_codes'),
                       'attempt': e.get('attempt_number'), 'status': e.get('execution_status'), 'result_only': e.get('result_only', False)}
                      for e in own_events if e.get('error_codes')]
            final_pictures = [picture(a) for a in finals]
            if finals:
                status = 'generated' if all(p['url'] for p in final_pictures) else 'missing'
            elif job.get('status') == 'failed':
                status = 'failed'
            elif sections and all(s.get('bucket') == 'exclude' for s in sections):
                status = 'excluded'
            elif any(e.get('result_only') and e.get('execution_status') == 'no_output' for e in own_events):
                status = 'no_output'
            else:
                status = 'unknown'
            sources.append({'key': source, 'status': status, 'original': picture(original), 'finals': final_pictures,
                            'sections': section_rows, 'errors': errors,
                            'result_records': [{'stage': e.get('stage'), 'status': e.get('execution_status'), 'result_only': e.get('result_only', False)}
                                               for e in own_events if e.get('result_only')]})
        if sources:
            result.append({'id': case, 'name': info.get('product_name') or job.get('product_name') or case,
                           'job_id': job.get('id'), 'job_status': job.get('status'), 'job_step': job.get('current_step'),
                           'errors': case_errors, 'sources': sources})
    represented = {s['key'] for c in result for s in c['sources']}
    if len(represented) != sum(len(c['sources']) for c in result):
        raise ValueError('서로 다른 상품에 같은 source_key가 연결됨')
    if represented != set(m.get('source_keys', [])):
        raise ValueError('manifest.source_keys와 원본/실행 증거의 대상 목록 불일치')
    counts = Counter(s['status'] for c in result for s in c['sources'])
    data = {'run_id': m['evaluation_run_id'], 'product_version': m.get('product_version'), 'product_commit': m.get('product_commit'),
            'reference_frozen': bool(m.get('reference_freeze')), 'cases': result, 'counts': dict(counts),
            'warnings': sorted(set(warnings)), 'read_only': True}
    report = {'run_id': data['run_id'], 'source_count': len(represented), 'case_count': len(result), 'status_counts': dict(counts),
              'final_images': sum(len(s['finals']) for c in result for s in c['sources']),
              'preview_images': sum(len(sec['previews']) for c in result for s in c['sources'] for sec in s['sections']),
              'section_count': sum(len(s['sections']) for c in result for s in c['sources']),
              'copied_unique_images': len(assets), 'warnings': data['warnings'], 'inputs': files.observed,
              'quality_evaluated': False}
    out.mkdir(parents=True)
    (out / 'assets').mkdir()
    for path, raw in assets.items():
        with (out / path).open('xb') as f:
            f.write(raw)
    templates = Path(__file__).with_name('viewer_assets')
    for name in ('viewer.js', 'viewer.css'):
        shutil.copyfile(templates / name, out / name)
    payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    html = (templates / 'index.html').read_text(encoding='utf-8').replace('__VIEWER_DATA__', payload)
    (out / 'index.html').write_text(html, encoding='utf-8')
    write_new(out / 'viewer-report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description='Baseline 읽기 전용 오프라인 비교 뷰어 생성')
    parser.add_argument('run', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--asset-root', action='append', default=[], type=Path, help='복원된 회차 상대 경로의 추가 자료 루트')
    parser.add_argument('--archive', nargs=2, action='append', default=[], metavar=('ZIP', 'SHA256'), help='다운로드된 ZIP과 기대 SHA-256; 자동 다운로드/추출하지 않음')
    args = parser.parse_args()
    try:
        result = generate(args.run, args.output, asset_roots=args.asset_root, archives=args.archive)
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as e:
        parser.exit(1, str(e) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'inputs'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
