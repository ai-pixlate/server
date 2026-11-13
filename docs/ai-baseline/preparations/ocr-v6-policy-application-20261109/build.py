"""승인된 제외 정책·원본 재확인 결과의 범위 검증. GT 및 CER는 변경하지 않음."""
from pathlib import Path
import json
import hashlib
import sys
from collections import Counter

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from tools.baseline.ocr_scope import filter_section, v5_inputs

P = Path(__file__).resolve().parent
B = ROOT / 'docs/ai-baseline'
OLD = B / 'preparations/ocr-v6-ai-scope-review-20261109'
UI = B / 'preparations/ocr-v6-review-ui-20261109'
VERSION = 'source-scope-v6-user-exclusions-20261109-1'

def read(p): return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(name, value): (P/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def main():
    protected = read(B/'analyses/ocr-scope-refinement-20261109/source-hashes.json')['protected']
    sources = read(OLD/'source-hashes.json')
    before_files = {str(p): sha(p) for folder in [OLD, UI] for p in folder.iterdir() if p.is_file()}
    for path, h in {**protected, **sources}.items(): assert sha(path) == h, path
    notes = read(UI/'user-review-chat.json')
    assert notes['data_sha256'] == sha(UI/'review-data.json')
    expected = {'GS-03_005__sec_1_01:scope', 'GS-03_014__sec_1_03:scope'}
    assert set(notes['records']) == expected and all(x['decision']=='exclude' for x in notes['records'].values())
    evidence = str(UI/'user-review-chat.json') + '#' + sha(UI/'user-review-chat.json')
    scopes = read(OLD/'fixed-regions.json')
    links = {x['section_id']: x for x in read(OLD/'gt-region-links.json')}
    previous = {x['section_id']: x for x in read(OLD/'section-status.json')}
    gt_path = B/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl'
    th = sha(gt_path)
    truth = {x['id']: x for x in map(json.loads, gt_path.read_text('utf-8').splitlines())}
    inputs = {x['id']: x for x in read(B/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json')['sections']}
    changes, audits, status, transitions = [], [], [], []
    # 원본에서 일반 설명의 온전한 의미 영역을 확인. OCR별 bbox에 맞춘 임계값 없음.
    repairs = {
        'GS-01_002__sec_1_11': ([0, 0, 1000, 810], '상단 두 줄 설명 전체 포함. 767.6 경계가 마지막 줄을 잘랐음. 아래 용기 인쇄는 제외 유지.'),
        'GS-02_014__sec_1_02': ([320, 100, 510, 175], '사진 위 how to use 전체 문구 포함. 기존 y=42.72~128.16이 문구 상단만 덮음. 얼굴과 용기는 제외 유지.'),
        'GS-03_008__sec_1_01': ([735, 75, 940, 190], '우측 성분 콜아웃 VITAMIN C 및 비타민C 모두 포함. 기존 y=134.26 시작은 영문 전체와 한글 상단을 누락.'),
    }
    for scope in scopes:
        sid = scope['id']; link = links[sid]
        scope['scope_version'] = link['scope_version'] = VERSION
        if sid in repairs:
            new, reason = repairs[sid]; old = scope['regions'][0]['rect']
            for loc in link['lines'].values():
                if loc.get('rect') == old:
                    loc.update(rect=new, reason=reason, anchor_kind='source_semantic_area_not_line_bbox')
            scope['regions'][0].update(rect=new, reason=reason)
            changes.append(dict(section_id=sid, before=old, after=new, reason=reason, source_image_sha256=scope['image_sha256']))
        if sid in ('GS-03_005__sec_1_01', 'GS-03_014__sec_1_03'):
            cream = sid == 'GS-03_005__sec_1_01'
            scope.update(section_type='mixed', default='exclude', coverage_verified=True)
            inc = [[70,200,930,730],[380,1785,620,1855]] if cream else [[60,85,650,200],[100,280,900,525],[610,850,850,1020],[220,1090,930,1200]]
            excluded = [0,750,1000,1780] if cream else [0,280,1000,850]
            scope['regions'] = [dict(id='E-user',kind='exclude',rect=excluded,verified=True,reason='사용자 제외 판정: 제형 숫자 3' if cream else '사용자 제외 판정: 옅은 배경 b3+',policy_evidence=evidence)]
            for n, rect in enumerate(inc):
                scope['regions'].append(dict(id=f'I{n+1}',kind='include',rect=rect,verified=True,overrides=[] if cream else ['E-user'],reason='원본에서 별도 설명·각주 확인. 배경 제외가 전경 설명을 지우지 않음'))
            scope['unresolved_connections'] = ['oversized_ocr_3_crosses_retained_footnote'] if cream else ['background_b3_overlaps_foreground_heading']
            rects = ([[380,205,620,280],[150,320,850,425],[70,425,930,535],[180,550,830,615],[190,615,820,675],[400,680,600,720],[140,850,900,1780],[400,1795,610,1845]] if cream else
                     [[70,95,470,125],[70,135,175,160],[70,160,205,188],[305,135,415,160],[305,160,475,188],[275,285,735,405],[105,410,890,515],[0,340,1000,850],[620,853,790,884],[620,884,840,915],[620,915,840,943],[620,943,840,973],[620,973,710,1010],[230,1105,915,1190]])
            assert len(rects) == len(link['lines'])
            for n, rect in enumerate(rects, 1):
                link['lines'][f'L{n:03d}'].update(rect=rect,verified=True,reason='원본 직접 대조; 전사·순서는 변경하지 않은 범위 연결',anchor_kind='source_visual_scope_rectangle')
            lid = 'L007' if cream else 'L008'
            link['lines'][lid].update(required_decision='exclude',policy_evidence=evidence)
            changes.append(dict(section_id=sid, excluded_gt=lid, policy_decision='exclude', policy_evidence=evidence, source_image_sha256=scope['image_sha256'], unresolved=scope['unresolved_connections']))
        ref = inputs[sid]['output']; raw = Path(ref['path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ref['sha256']
        payload = json.loads(raw.decode().splitlines()[ref['line']-1] if ref.get('line') else raw)
        for key in ref['pointer'].split('/')[1:]: payload = payload[int(key)] if isinstance(payload, list) else payload[key]
        out = []
        for n, r in enumerate(payload['regions']):
            b = r.get('bbox')
            out.append(dict(id=f"{n}:{r['region_key']}",text=r['text'],rect=[b['x'],b['y'],b['x']+b['w'],b['y']+b['h']] if b else None,mixed=sid=='GS-01_002__sec_1_23' and r['region_key']=='reg_0001'))
        gt = [dict(id=x['line_id'],text=x['text']) for x in truth[sid]['lines']]
        result = filter_section(scope, gt, out, image_sha256=scope['image_sha256'], gt_sha256=th, gt_links=link)
        assert len(result['audit']) == len(gt)+len(out)
        assert [x['text'] for x in result['audit'] if x['kind']=='gt'] == [x['text'] for x in gt]
        assert [x['text'] for x in result['audit'] if x['kind']=='ocr'] == [x['text'] for x in out]
        if result['ready'] and result['applicability']=='applicable': v5_inputs(result)
        audits.append(dict(section_id=sid, **result))
        label = '검증 완료' if result['ready'] else '기술적 연결 차단'
        status.append(dict(section_id=sid,status=label,filter_ready=result['ready'],policy_user_pending=False,official_scoring_authorized=False,blockers=result['blockers']))
    old_audits = {x['section_id']:x for x in read(OLD/'filter-audit.json')}
    for row in audits:
        old = {(x['kind'],x['id']):x for x in old_audits[row['section_id']]['audit']}
        for a in row['audit']:
            prev = old[(a['kind'],a['id'])]
            if a['decision'] != prev['decision']:
                transitions.append(dict(section_id=row['section_id'],kind=a['kind'],id=a['id'],text=a['text'],before=prev['decision'],after=a['decision'],reason=a['reason']))
    for path,h in {**protected,**sources,**before_files}.items(): assert sha(path)==h,path
    assert {x['section_id'] for x in status if x['filter_ready']} >= {sid for sid,x in previous.items() if x['filter_ready']}
    assert len(status) == 112 and len({x['section_id'] for x in status}) == 112
    summary = dict(before=dict(Counter(x['status'] for x in previous.values())),after=dict(Counter(x['status'] for x in status)),
        newly_ready=[x['section_id'] for x in status if x['filter_ready'] and not previous[x['section_id']]['filter_ready']],
        blockers=dict(Counter(b.split(':')[-1] for x in status for b in x['blockers'])),policy_questions_remaining=0,
        protected_files_unchanged=len(protected),source_hashes_verified=len(sources),prior_preparation_files_unchanged=len(before_files),
        source_visual_reviewed_this_pass=list(repairs)+['GS-03_005__sec_1_01','GS-03_014__sec_1_03'],cer_called=False,gt_modified=False)
    for name,value in [('fixed-regions.json',scopes),('gt-region-links.json',list(links.values())),('section-status.json',status),('filter-audit.json',audits),('scope-changes.json',changes),('decision-transitions.json',transitions),('validation.json',summary)]:write(name,value)
    write('provenance.json',dict(user_notes_sha256=sha(UI/'user-review-chat.json'),truth_sha256=th,filter_sha256=sha(ROOT/'tools/baseline/ocr_scope.py'),source_hashes=sources,previous_scopes_sha256=sha(OLD/'fixed-regions.json')))
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
