"""v1.0.1의 공개 검수 함수·채택 산출물을 수집기에 연결. import 시 제품 로드 없음."""
import json
from pathlib import Path


class ProductReview:
    def __init__(self,jid,seller,sessions,query,dispatch,store,source_bindings):
        self.jid,self.seller,self.sessions,self.query,self.dispatch,self.store,self.bindings=jid,seller,sessions,query,dispatch,store,source_bindings

    def snapshot(self,stage):
        from app.flows import common, final
        from app.routers import cfm
        with self.sessions() as db:
            sections=common.visible_sections(db,self.jid)
            by_section={s['id']:self.bindings[s['source_image_id']] for s in sections}
            def local(key):
                return str(self.store.root/self.store.index[key]['sha256']) if key and self.store.exists(key) else None
            images=[{'section_id':s['id'],'source_key':by_section[s['id']],
                     'original':local(s.get('image_key')),'preview':local(s.get('render_image_key'))} for s in sections]
            if stage=='N3':
                return {'values':{str(s['id']):s['bucket'] for s in sections},
                        'source_by_target':{str(s['id']):by_section[s['id']] for s in sections},
                        'sections':sections,'images':images,'warnings':[]}
            blocks=cfm._blocks(db,self.jid)
            return {'values':{str(b['id']):b['trans1'] or '' for b in blocks},
                    'source_by_target':{str(b['id']):by_section[b['sectionId']] for b in blocks},
                    'blocks':blocks,'sections':sections,'images':images,'warnings':final.unresolved_warnings(db,self.jid)}

    def apply(self,stage,answer,state):
        from app.routers import sections, cfm
        target=answer.get('target_id')
        if target not in state['values']: raise ValueError('검수 대상 밖 수정')
        with self.sessions() as db:
            if stage=='N3':
                value=answer.get('value')
                if value not in ('include','exclude'): raise ValueError('포함·제외 값 필요')
                sections.update_section(self.jid,int(target),sections.SectionAction(action='restore' if value=='include' else 'exclude'),db,self.seller)
            else:
                if not isinstance(answer.get('value'),str): raise ValueError('번역 문자열 필요')
                block=next(b for b in state['blocks'] if str(b['id'])==target)
                cfm.update_block(self.jid,int(target),cfm.BlockUpdate(trans1=answer['value'],revision=block['revision']),db,self.seller)
        self.dispatch.drain()  # 제품의 수정 후 미리보기 재렌더만; 사용자 재생성 없음

    def files(self,checkpoint,state):
        from PIL import Image
        items=[]
        if checkpoint=='machine_snapshot':
            candidates=[(s['source_image_id'],s.get('render_image_key'),'preview_section',s['id']) for s in state['sections'] if s['bucket']=='include']
        else:
            candidates=[(d['source_image_id'],d['image_url'],'final_image',d['id']) for d in self.query('SELECT * FROM deliverable WHERE job_id=:j ORDER BY id',j=self.jid)]
        for source,key,kind,target in candidates:
            if not key or not self.store.exists(key): continue  # 원본 이미지를 성공 미리보기로 대체하지 않음
            entry=self.store.index[key]; path=self.store.root/entry['sha256']
            with Image.open(path) as image: width,height=image.size; media=Image.MIME.get(image.format,'application/octet-stream')
            items.append({'source_key':self.bindings[source],'target_id':target,'path':str(path),'kind':kind,'width':width,'height':height,'media_type':media})
        return items

    def save(self):
        from app.routers import finalize
        with self.sessions() as db: return finalize.save(self.jid,db,self.seller)


    def regulatory_evidence(self, collector, state):
        verdicts=self.query("SELECT v.*, d.dict_type FROM section_verdict v JOIN section s ON s.id=v.section_id LEFT JOIN expression_dictionary d ON d.id=v.dictionary_id WHERE s.job_id=:j",j=self.jid)
        for section in state['sections']:
            original=section.get('original_verdict')
            if isinstance(original,str): original=json.loads(original)
            if not original: continue  # 미수집을 판정 실패로 바꾸지 않는다
            measured=original.get('regulatory_status')=='ok'
            matches=[v for v in verdicts if v['section_id']==section['id'] and v['dict_type']=='regulatory' and v['verdict_status']!='allowed']
            collector.operation('regulation_checked','N2',dict(target_id=str(section['id']),measured=measured,
                findings=len(matches),auto_excluded=original.get('bucket')=='exclude',locale_unchecked=original.get('local_status')!='ok'))

    def spec_evidence(self,collector):
        for d in self.query('SELECT id, validation_result FROM deliverable WHERE job_id=:j',j=self.jid):
            value=d['validation_result']
            if isinstance(value,str):value=json.loads(value)
            if isinstance(value,dict) and type(value.get('passed')) is bool:
                collector.operation('spec_checked','N6',dict(target_id=str(d['id']),passed=value['passed']))
