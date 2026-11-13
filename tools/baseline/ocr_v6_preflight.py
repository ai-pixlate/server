"""실제 입력 검증 전용 CLI. 문자 채점 함수 호출을 금지한다."""
import argparse
import json
from pathlib import Path
from hashlib import sha256
from unittest.mock import patch
from . import ocr_v6


def read(p): return json.loads(Path(p).read_text('utf-8-sig'))
def file_hash(p): return sha256(Path(p).read_bytes()).hexdigest()


def keyed(rows,key):
    result={r[key]:r for r in rows}
    if len(result)!=len(rows): raise ValueError('duplicate_id')
    return result


def preflight(package,truth,index,spec,image_root):
    package,truth,index,spec=map(Path,(package,truth,index,spec))
    provenance=read(package/'provenance.json')['immutable']
    before={p:file_hash(p) for p in provenance}
    if before!=provenance: raise ValueError('protected_evidence_hash_mismatch')
    paths=[package/n for n in ('fixed-regions.json','gt-region-links.json','section-status.json')]+[truth,index,spec,package/'filter-audit.json']
    inputs={str(p):file_hash(p) for p in paths}
    scopes=keyed(read(paths[0]),'id'); links=keyed(read(paths[1]),'section_id')
    statuses=keyed(read(paths[2]),'section_id')
    units=keyed([json.loads(x) for x in truth.read_text('utf8').splitlines()],'id')
    outputs=keyed(read(index)['sections'],'id')
    audits=keyed(read(package/'filter-audit.json'),'section_id')
    if not all(set(x)==set(scopes) for x in (links,statuses,units,outputs,audits)):
        raise ValueError('section_coverage_mismatch')
    rows=[]
    with patch.object(ocr_v6,'score_lines',side_effect=AssertionError('실제 채점 금지')):
        for sid,s in scopes.items():
            u=units[sid];entry=outputs[sid];ref=entry['output']
            truth_image=Path(u['image']['path'])
            if not truth_image.is_absolute():truth_image=Path(image_root)/truth_image
            if file_hash(s['image_path'])!=s['image_sha256'] or file_hash(truth_image)!=s['image_sha256'] or s['image_sha256']!=u['image']['sha256'] or s['image_sha256']!=entry['input_sha256']:
                raise ValueError('input_image_mismatch:'+sid)
            if entry['boundary']!='pipeline.stages.ocr.run:return':raise ValueError('output_boundary_mismatch')
            for r in (ref,entry['evidence']):
                if file_hash(r['path'])!=r['sha256']:raise ValueError('output_evidence_mismatch:'+sid)
            raw=Path(ref['path']).read_bytes()
            data=json.loads(raw.decode().splitlines()[ref['line']-1] if ref.get('line') else raw)
            for key in ref['pointer'].split('/')[1:]:data=data[int(key)] if isinstance(data,list) else data[key]
            out=[]
            mixed_ids={r['id'] for r in audits[sid]['audit'] if r['kind']=='ocr' and r['reason']=='mixed_string_no_split'}
            for n,r in enumerate(data['regions']):
                b=r.get('bbox')
                out.append(dict(id=f"{n}:{r['region_key']}",text=r['text'],
                    rect=[b['x'],b['y'],b['x']+b['w'],b['y']+b['h']] if b else None,
                    mixed=f"{n}:{r['region_key']}" in mixed_ids))
            metadata=dict(group=u['sample'].split('_')[0],input_version='ocr-section-input-v1',
                truth_version=truth.parent.name,product_version=entry['product_version'],product_sha=entry['product_sha'],
                image_sha256=s['image_sha256'],truth_sha256=inputs[str(truth)],output_sha256=ref['sha256'],
                policy_sha256=inputs[str(paths[0])],spec_sha256=inputs[str(spec)],output_locator=ref,
                origin=entry['origin'],output_boundary=entry['boundary'],scope_version=s['scope_version'],policy_version=s['policy_version'])
            row=ocr_v6.prepare_section(s,[dict(id=x['line_id'],text=x['text']) for x in u['lines']],out,links[sid],
                metadata=metadata,gt_complete=False,scope_approved=False)
            if row['filter_ready']!=statuses[sid]['filter_ready']:raise ValueError('filter_regression:'+sid)
            if row['filter']['audit']!=audits[sid]['audit']:raise ValueError('output_link_or_scope_changed:'+sid)
            assert row['metrics'] is None and not row['score_ready']
            rows.append(row)
    for p,h in {**before,**inputs}.items():
        if file_hash(p)!=h:raise ValueError('input_changed:'+p)
    return dict(mode='preflight_only',cer_called=False,sections=rows,
        summary=dict(total=len(rows),filter_ready=sum(r['filter_ready'] for r in rows),
                     filter_blocked=sum(not r['filter_ready'] for r in rows),score_ready=0,
                     scope_approval_pending=len(rows),gt_completeness_certified=0),
        hashes=inputs,protected_hashes_verified=len(before),implementation=ocr_v6.implementation())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('package','truth','index','spec','output','image-root'):parser.add_argument('--'+key,required=True,type=Path)
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('기존 결과 덮어쓰기 금지')
    result=preflight(args.package,args.truth,args.index,args.spec,args.image_root)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps(result['summary'],ensure_ascii=False))


if __name__=='__main__':main()
