"""N3·N5 사람 입력 대기와 불변 수집. 모듈 import는 제품/모델에 접근하지 않는다."""
import copy
import time
import uuid
from pathlib import Path

from . import protocol as p
from .core import now, read, write_new, digest
from .scope import automatic_only


class HumanRequired(BaseException):
    pass


def terminal_response(request_path):
    print(f'사람 검수 요청: {request_path}\nJSON 응답 파일 경로를 입력하세요. 빈 입력은 중단합니다.')
    path = input().strip().strip('"')
    if not path: raise HumanRequired('사람 입력 없음')
    return read(Path(path))


class Collector:
    def __init__(self, root, job_id, sources):
        self.root = Path(root)
        self.manifest = read(self.root / 'manifest.json')
        self.job_id, self.sources = job_id, sources
        if not sources or not set(sources) <= set(self.manifest['source_keys']): raise ValueError('원본 연결 오류')
        if len({s[:5] for s in sources}) != 1: raise ValueError('상품 작업 혼합 금지')

    def evidence(self, data):
        path = self.root / 'artifacts' / (uuid.uuid4().hex + '.json')
        write_new(path, data)
        return {'path': path.relative_to(self.root).as_posix(), 'sha256': digest(path)}

    def event(self, kind, stage, evidence, **kw):
        row = dict.fromkeys(p.FIELDS['execution'].split())
        row.update(evaluation_run_id=self.manifest['evaluation_run_id'],record_id=uuid.uuid4().hex,
                   record_kind=kind,case_id=self.sources[0][:5],source_keys=self.sources,job_id=self.job_id,
                   stage=stage,started_at=now(),ended_at=now(),evidence_refs=[evidence])
        row.update(kw)
        for key in ('started_at','ended_at'):
            if hasattr(row[key], 'isoformat'): row[key]=row[key].isoformat()
        p.record(self.root,'execution',row)
        return row

    def operation(self,event,stage,values, evidence=None):
        return self.event('operation',stage,evidence or self.evidence(values),event=event,values=values)

    def claim(self):
        # 같은 회차의 상품 생성은 프로세스가 중단돼도 자동 재실행하지 않는다.
        path=self.root/'generation-claims'/self.sources[0][:5]
        path.mkdir(parents=True,exist_ok=False)
        write_new(path/'claim.json',{'job_id':self.job_id,'source_keys':self.sources,'at':now()})

    def checkpoint(self, checkpoint, state, files):
        evidence=self.evidence({'checkpoint':checkpoint,'state':state})
        ids=[]
        for item in files:
            meta=dict(artifact_id=uuid.uuid4().hex,case_id=self.sources[0][:5],source_keys=[item['source_key']],
                      kind=item['kind'],stage='N5' if checkpoint=='machine_snapshot' else 'N6',checkpoint=checkpoint,
                      media_type=item['media_type'],width=item['width'],height=item['height'],parent_artifact_id=None,
                      crop_bbox=None,created_at=now(),unavailable_reason=None,target_id=item.get('target_id'),state_ref=evidence)
            ids.append(p.capture_artifact(self.root,meta,item['path'])['artifact_id'])
        # 평가자가 원본별 섹션 순서·산출물 ID·N3 개입을 추적할 수 있게 별도 색인 보존.
        self.evidence({'checkpoint':checkpoint,'artifact_ids':ids,'state_ref':evidence,'files':files})
        return ids


class HumanSession:
    """adapter.snapshot/apply는 기존 제품 함수를 사용한다. 응답 없으면 진행하지 않는다."""
    def __init__(self, collector, adapter, response=terminal_response, clock=time.monotonic):
        self.c,self.adapter,self.response,self.clock=collector,adapter,response,clock

    def review(self,stage):
        automatic = automatic_only(self.c.manifest)
        initial=copy.deepcopy(self.adapter.snapshot(stage))
        original=copy.deepcopy(initial['values'])
        entered=self.clock()
        while True:
            state=self.adapter.snapshot(stage)
            if automatic and state['values'] != original:
                raise HumanRequired('무수정 회차의 최초 값 변경 감지')
            request={'request_id':uuid.uuid4().hex,'job_id':self.c.job_id,'stage':stage,
                     'state':state,'instructions':'직접 검수 후 edit 또는 continue. 경고 확인 목록은 직접 지정. 재생성은 지원하지 않음.'}
            if automatic:
                request['instructions'] = '무수정 회차: 최초 값을 변경하지 않고 continue로 직접 확인. 경고 확인 목록은 사람이 지정. edit·재생성 금지.'
            evidence=self.c.evidence(request)
            answer=self.response(self.c.root/evidence['path'])
            if not isinstance(answer,dict) or answer.get('request_id')!=request['request_id'] or not answer.get('actor'):
                raise HumanRequired('요청 ID 또는 검수자 불일치')
            if automatic:
                from .scope import CONFIRMATION_ACTOR
                if answer['actor'] != CONFIRMATION_ACTOR:
                    raise HumanRequired('N3/N5 확인 담당자는 예람')
            action=answer.get('action')
            if action not in ('edit','continue'): raise HumanRequired('지원하지 않는 사람 행동')
            if self.adapter.snapshot(stage)!=state: raise HumanRequired('검수 중 제품 상태 변경; 오래된 응답 사용 금지')
            reply_ref=self.c.evidence(answer)
            if action=='edit':
                if automatic:
                    raise HumanRequired('무수정 회차에서는 편집할 수 없음')
                self.adapter.apply(stage,answer,state)
                self.c.evidence({'request':evidence,'response':reply_ref,'after':self.adapter.snapshot(stage)})
                continue
            if stage=='N5' and not isinstance(answer.get('acknowledgedWarnings'),list):
                raise HumanRequired('명시적인 경고 확인 목록 필요(없으면 빈 배열)')
            final=state['values']
            if original.keys()!=final.keys(): raise HumanRequired('검수 대상 변경')
            for source in self.c.sources:
                keys=[k for k in original if state['source_by_target'][k]==source]
                before={k:original[k] for k in keys}; after={k:final[k] for k in keys}
                changed=sum(before[k]!=after[k] for k in keys)
                self.c.event('user_edit',stage,reply_ref,source_keys=[source],source_key=source,target_id=source,
                             original_value=before,final_value=after,change_count=changed,user_retry_count=0,
                             confirmed_at=now(),n3_intervention=('selection_changed' if changed else 'confirmation_only') if stage=='N3' else None)
            self.c.operation('human_wait',stage,{'seconds':self.clock()-entered},reply_ref)
            self.c.evidence({'initial':initial,'final':state,'request':evidence,'response':reply_ref})
            return {'original':original,'final':final,'acknowledgedWarnings':answer.get('acknowledgedWarnings',[]),'evidence':reply_ref}
