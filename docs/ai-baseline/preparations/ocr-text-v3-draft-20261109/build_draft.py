"""이미지 독립 판독으로 작성한 proposals를 별도 미동결 패키지로 구성한다.
OCR 산출물과 CER은 읽지 않는다. 전사 생성/추론 코드가 아니다.
"""
from pathlib import Path
import copy
import hashlib
import json

P=Path(__file__).resolve().parent
W=P.parents[3]
def read(p): return json.loads(p.read_text(encoding='utf8'))
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def text_hash(lines): return hashlib.sha256(json.dumps(lines,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def main():
    targets=read(P/'targets.json'); proposals=read(P/'proposals.json')
    assert {u['id'] for u in targets}==set(proposals) and len(targets)==18
    out=P/'drafts'
    out.mkdir(exist_ok=False)
    records=[]
    for u in targets:
        sid=u['id']; p=proposals[sid]
        before=[{'line_id':l['line_id'],'text':l['text']} for l in u['lines']]
        lines=copy.deepcopy(before); changes=[]
        for edit in p['edits']:
            l=next(x for x in lines if x['line_id']==edit['id'])
            if l['text']!=edit['text']:
                changes.append({'operation':'replace','line_id':l['line_id'],'before':l['text'],'after':edit['text'],'reason':edit['reason']})
                l['text']=edit['text']
        next_id=max(int(l['line_id'][1:]) for l in lines)+1
        for insert in p['insertions']:
            pos=next(i for i,l in enumerate(lines) if l['line_id']==insert['after'])+1
            extra=[]
            for text in insert['texts']:
                lid=f'L{next_id:03}';next_id+=1
                extra.append({'line_id':lid,'text':text})
                changes.append({'operation':'insert','line_id':lid,'before':None,'after':text,'after_existing_id':insert['after'],'reason':insert['reason']})
            lines[pos:pos]=extra
        image=copy.deepcopy(u['image'])
        if not Path(image['path']).is_absolute(): image['path']=str(Path('C:/Users/Yeram/Documents/pixlate-server')/image['path'])
        assert sha(Path(image['path']))==image['sha256']
        assert len({l['line_id'] for l in lines})==len(lines)
        d=dict(id=sid,sample=u['sample'],section_key=u['section_key'],image=image,
               version='ocr-text-v3-draft-20261109',status='needs_attention',revision=1,
               parent_version='ocr-text-v2-20261109',parent_lines=before,parent_text_sha256=text_hash(before),
               lines=lines,text_sha256=text_hash(lines),changes=changes,unresolved=p['unresolved'],
               note=p['problem']+'\n'+'\n'.join(p['unresolved']),
               read_only=False,human_reviews=[],approved=False,frozen=False,
               ai_review={'method':'Codex chat original-image visual review','exact_model_settings':'unverified',
                          'source':'original section image only; parent GT retained when not changed',
                          'ocr_output_used':False,'cer_used':False,'complete':False,'status':'needs_review'},
               history=[{'at':'2026-11-09','by':'Codex chat','before':{'lines':before,'status':'frozen_v2_parent'},
                         'after':{'lines':lines,'status':'needs_attention'},'reason':'원본 이미지 독립 대조; 사용자 승인 전 보완 후보'}])
        file=out/(sid+'.json');file.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        records.append({'id':sid,'path':'drafts/'+file.name,'sha256':sha(file),'revision':1,'text_sha256':d['text_sha256'],
                        'changes':len(changes),'unresolved':len(p['unresolved'])})
    manifest={'version':'ocr-text-v3-draft-20261109','status':'unfrozen_draft','approved':False,'frozen':False,
              'parent_truth_sha256':sha(W/'docs/ai-baseline/references/ocr-text/ocr-text-v2-20261109/text_units.jsonl'),
              'scope':18,'inherited_other_94':'not_copied_or_modified','human_reviews':0,'sections':records,
              'input_files':{f:sha(P/f) for f in ['targets.json','proposals.json','build_draft.py']}}
    (P/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({'sections':18,'changed_sections':sum(bool(r['changes']) for r in records),'changes':sum(r['changes'] for r in records)}))

if __name__=='__main__': main()
