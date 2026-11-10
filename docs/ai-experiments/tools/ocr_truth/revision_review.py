"""기존 텍스트 검수 UI/저장 검증을 재사용하는 별도 버전 보완 작업 공간."""
import argparse
import copy
import hashlib
import json
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
from text_review import Workspace, digest, handler_class
from truth import Handler, HERE, now

def text_hash(lines):
    return hashlib.sha256(json.dumps(lines,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

class RevisionWorkspace(Workspace):
    def __init__(self, package, drafts):
        self.package=Path(package).resolve(); self.drafts=Path(drafts).resolve()
        if '.cache' not in self.drafts.parts or self.drafts.is_relative_to(self.package) or self.package.is_relative_to(self.drafts):
            raise ValueError('검수 저장은 원본 패키지 밖 별도 .cache 경로 필요')
        self.inventory=self.package/'manifest.json'; self.inventory_hash=digest(self.inventory)
        manifest=json.loads(self.inventory.read_text(encoding='utf8'))
        if manifest['status']!='unfrozen_draft' or manifest['approved'] or manifest['frozen']:
            raise ValueError('미승인·미동결 초안만 허용')
        self.lock=threading.RLock(); self.rows={};self.seed={}
        for entry in manifest['sections']:
            p=(self.package/entry['path']).resolve()
            if not p.is_relative_to(self.package) or digest(p)!=entry['sha256']:
                raise ValueError('초안 경로/해시 오류')
            d=json.loads(p.read_text(encoding='utf8'));tid=d['id']
            if tid in self.rows or tid!=entry['id'] or text_hash(d['lines'])!=entry['text_sha256']:
                raise ValueError('초안 ID/텍스트 연결 오류')
            self.rows[tid]={**d['image'],'source_id':d['sample'],'section_key':d['section_key']}
            self.validate_image(self.rows[tid]);self.seed[tid]=d
        if len(self.rows)!=manifest['scope']:raise ValueError('대상 누락')
        for tid in self.rows:self.load(tid)

    def load(self,tid):
        if tid not in self.seed:raise KeyError(tid)
        p=self.path(tid)
        d=json.loads(p.read_text(encoding='utf8')) if p.exists() else copy.deepcopy(self.seed[tid])
        if d['id']!=tid or d['image']!=self.seed[tid]['image'] or (p.exists() and d['inventory_sha256']!=self.inventory_hash):
            raise ValueError('검수 초안 연결 오류')
        if text_hash(d['lines'])!=d['text_sha256']:raise ValueError('텍스트 해시 오류')
        d['inventory_sha256']=self.inventory_hash
        d['human_review_current']=any(r['revision']==d['revision'] and r['text_sha256']==d['text_sha256'] for r in d['human_reviews'])
        return d

    def save(self,tid,body,clock,actor='human'):
        if actor!='human':raise ValueError('이 화면은 사용자 검수 저장 전용')
        return super().save(tid,body,clock,actor)

    def finalize_saved_revision(self,d,clock,actor):
        d['text_sha256']=text_hash(d['lines'])
        d['approved']=False;d['frozen']=False
        if d['status']=='reviewed':
            d['human_reviews'].append({'at':clock(),'revision':d['revision'],'text_sha256':d['text_sha256'],
                'image_sha256':d['image']['sha256'],'by':'user','note':d['note'],'approval':False,'freeze':False})
        d['human_review_current']=d['status']=='reviewed'

    def listing(self):
        d=super().listing();d['revision_mode']=True
        return d

def make_handler(workspace):
    base=handler_class(Handler,workspace,HERE,now)
    class RevisionHandler(base):
        def do_POST(self):
            if not self.path.startswith('/api/truth/'):
                return self._json(403,{'error':'보완 화면은 사용자 초안 저장만 허용'})
            return super().do_POST()
    return RevisionHandler

def main():
    p=argparse.ArgumentParser();p.add_argument('--package',required=True);p.add_argument('--drafts',required=True)
    p.add_argument('--port',type=int,default=8767);p.add_argument('--check-only',action='store_true');a=p.parse_args()
    w=RevisionWorkspace(a.package,a.drafts)
    print(json.dumps(w.listing()['progress']),flush=True)
    if a.check_only:return
    with ThreadingHTTPServer(('127.0.0.1',a.port),make_handler(w)) as server:
        print(f'OCR Text v3 초안 검수: http://127.0.0.1:{a.port}/',flush=True)
        server.serve_forever()

if __name__=='__main__': main()
