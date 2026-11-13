"""동결 파일·선택적 외부 증거 무결성 검증. OCR/CER 호출 없음."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-inputs',action='store_true')
    parser.add_argument('--check-source-evidence',action='store_true')
    args=parser.parse_args()
    base=Path(__file__).resolve().parent
    def read(name):return json.loads((base/name).read_text('utf8'))
    def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    manifest=read('manifest.json')
    actual={p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file() and p.name!='manifest.json'}
    assert actual==set(manifest['files']),'파일 추가/누락'
    for name,h in manifest['files'].items():assert sha(base/name)==h,name
    plan=read('evaluation-plan.json');assert plan['frozen'] is True
    groups=[plan[k] for k in ('included','not_applicable','undetermined')]
    assert list(map(len,groups))==[105,4,3]
    ids=[sid for group in groups for sid in group];assert len(set(ids))==112
    units=[json.loads(x) for x in (base/'text_units.jsonl').read_text('utf8').splitlines()]
    assert set(ids)=={x['id'] for x in units}
    assert sum(len(x['lines']) for x in units)==1861
    assert [(u['id'],l['line_id']) for u in units for l in u['lines'] if l.get('active') is False]==[('GS-02_002__sec_1_01','L005'),('GS-02_002__sec_1_01','L006')]
    assert next(l['text'] for u in units if u['id']=='GS-02_001__sec_1_03' for l in u['lines'] if l['line_id']=='L045')=='상품앞 @표시품목 : 부가세 면세품목'
    assert sha(base/'text_units.jsonl')==plan['truth_sha256']
    links={x['section_id']:x for x in read('gt-region-links.json')}
    for u in units:
        assert links[u['id']]['truth_sha256']==plan['truth_sha256']
        for l in u['lines']:assert links[u['id']]['lines'][l['line_id']]['text_sha256']==hashlib.sha256(l['text'].encode()).hexdigest()
    if args.check_inputs:
        for r in read('inputs.json'):assert sha(r['image_path'])==r['image_sha256'],r['id']
    if args.check_source_evidence:
        for path,h in read('source-evidence-hashes.json').items():assert sha(path)==h,path
    print('PASS: 동결 파일·105/4/3·GT/연결 해시; CER 실행 없음')


if __name__=='__main__':main()
