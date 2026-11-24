"""과거 저장 JSON만 읽는 비용 근거 요약. CountTokens·생성 API를 사용하지 않는다."""
import json
from pathlib import Path

from .core import read, digest


def historical(root):
    root = Path(root)
    split_files = sorted((root / 'pipeline/samples/local/merge-input-v1').glob('*/upstream_ocr_input_v1/origin/split_debug.json'))
    calls, sections, source_ids, evidence = 0, 0, [], []
    for f in split_files:
        d = read(f)
        calls += sum(len(v['calls']) for v in d['vlm'])
        sid = f.parents[2].name
        source_ids.append(sid)
        split = f.parents[2] / 'split.json'
        sections += len(read(split)['sections'])
        evidence.append({'path': f.relative_to(root).as_posix(), 'sha256': digest(f)})
    fixed = read(root / 'pipeline/samples/evaluation/gs-v1/manifest.json')
    missing = [x for x in fixed['files'] if x['source_id'] not in source_ids]
    extra_s = sum(max(1, x['height'] // 200) for x in missing)
    extra_v = sum(x['height'] // 1501 for x in missing)
    return {'observed_sources': source_ids, 'observed_split_calls': calls, 'observed_sections': sections,
            'missing_sources': [x['source_id'] for x in missing], 'missing_section_bound': extra_s,
            'missing_split_bound': extra_v,
            'scenario_initial_calls': [calls + 4 * sections, calls + extra_v + 4 * (sections + extra_s)],
            'scenario_retry_calls': 3 * (calls + extra_v) + 10 * (sections + extra_s),
            'caveat': '과거 32장 분할 유지 가정. 현재 출력·성공·전체 비용 예측 아님',
            'evidence': evidence, 'executed': False}
