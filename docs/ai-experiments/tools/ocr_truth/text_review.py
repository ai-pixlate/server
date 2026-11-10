"""텍스트 전용 검수 작업 공간. 모델 호출·채점·동결 기능은 없다."""
import copy
import hashlib
import json
import re
import threading
from pathlib import Path
from PIL import Image
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_review


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Workspace:
    def __init__(self, inventory, frozen, drafts, image_root=None):
        self.inventory = Path(inventory).resolve()
        self.frozen = Path(frozen).resolve()
        self.drafts = Path(drafts).resolve()
        # 초안 저장은 격리 작업 공간에서만 허용한다.
        if '.cache' not in self.drafts.parts or self.drafts.is_relative_to(self.frozen) or self.frozen.is_relative_to(self.drafts) or self.inventory.is_relative_to(self.drafts):
            raise ValueError('초안 폴더는 별도 .cache 하위여야 합니다')
        self.lock = threading.Lock()
        self.inventory_hash = digest(self.inventory)
        manifest = json.loads((self.frozen / 'manifest.json').read_text(encoding='utf-8'))
        if manifest['status'] != 'frozen_text_only':
            raise ValueError('텍스트 동결본이 아닙니다')
        for name, sha in manifest['files'].items():
            p = (self.frozen / name).resolve()
            if not p.is_relative_to(self.frozen) or digest(p) != sha:
                raise ValueError('동결본 해시 불일치')
        units = [json.loads(x) for x in (self.frozen / 'text_units.jsonl').read_text(encoding='utf-8').splitlines()]
        self.units = {u['id']: u for u in units}
        if len(self.units) != len(units) or set(self.units) != set(manifest['sections']):
            raise ValueError('동결 ID 중복/누락')
        data = json.loads(self.inventory.read_text(encoding='utf-8'))
        self.rows = {}
        hashes = set()
        for row in data['sections']:
            tid = row['source_id'] + '__' + row['section_key']
            if not re.fullmatch(r'[A-Za-z0-9_-]+', tid) or tid in self.rows or row['sha256'] in hashes:
                raise ValueError('섹션 ID/이미지 중복')
            row = copy.deepcopy(row)
            path = Path(row['path'])
            if image_root:
                path = Path(image_root) / 'pipeline/out/ocr_input/v1' / row['source_id'] / 'sections' / (row['section_key'] + '.png')
            row['path'] = str(path.resolve())
            self.validate_image(row)
            fid = row.get('truth_v1_id')
            if fid:
                u = self.units.get(fid)
                if not u or fid != tid or u['image']['sha256'] != row['sha256'] or (u['image']['width'], u['image']['height']) != (row['width'], row['height']):
                    raise ValueError('동결 정답 연결 불일치')
            self.rows[tid] = row
            hashes.add(row['sha256'])
        if len(self.rows) != data['summary']['sections'] or len({r['source_id'] for r in self.rows.values()}) != data['summary']['sources']:
            raise ValueError('섹션/원본 누락')
        if {r['truth_v1_id'] for r in self.rows.values() if r.get('truth_v1_id')} != set(self.units):
            raise ValueError('동결 정답 누락')
        for tid in self.rows:
            self.load(tid)  # 기존 초안의 다른 입력 연결을 시작 전에 차단

    def validate_image(self, row):
        p = Path(row['path'])
        if not p.is_file() or digest(p) != row['sha256']:
            raise ValueError('이미지 누락/SHA-256 불일치: ' + str(p))
        with Image.open(p) as im:
            if im.size != (row['width'], row['height']):
                raise ValueError('이미지 해상도 불일치')

    def path(self, tid):
        if tid not in self.rows:
            raise KeyError(tid)
        p = (self.drafts / (tid + '.json')).resolve()
        if not p.is_relative_to(self.drafts):
            raise ValueError('작업 공간 밖 저장 금지')
        return p

    def load(self, tid):
        row = self.rows[tid]
        if row.get('truth_v1_id'):
            u = copy.deepcopy(self.units[row['truth_v1_id']])
            return {**u, 'read_only': True, 'status': 'frozen_v1', 'revision': 0, 'note': '', 'history': []}
        p = self.path(tid)
        if p.exists():
            d = json.loads(p.read_text(encoding='utf-8'))
            if d['id'] != tid or d['image']['sha256'] != row['sha256'] or d['inventory_sha256'] != self.inventory_hash:
                raise ValueError('초안 입력 연결 불일치')
            return d
        return {'id': tid, 'sample': row['source_id'], 'section_key': row['section_key'],
                'image': {k: row[k] for k in ('path', 'sha256', 'width', 'height')},
                'inventory_sha256': self.inventory_hash, 'read_only': False, 'status': 'empty',
                'revision': 0, 'lines': [], 'note': '', 'history': []}

    def save(self, tid, body, now, actor="human"):
        with self.lock:
            d = self.load(tid)
            if d['read_only']:
                raise PermissionError('동결된 v1 정답은 읽기 전용입니다')
            if body.get('revision') != d['revision']:
                raise ValueError('다른 창에서 저장했습니다. 새로 불러오세요')
            self.validate_image(self.rows[tid])
            lines = body['lines']
            if not isinstance(lines, list) or len(lines) > 10000:
                raise ValueError('줄 목록 형식 오류')
            ids = set()
            for line in lines:
                if not isinstance(line, dict) or not re.fullmatch(r'L[0-9]+', str(line.get('line_id', ''))) or line['line_id'] in ids or not isinstance(line.get('text'), str):
                    raise ValueError('줄 ID 중복 또는 텍스트 형식 오류')
                ids.add(line['line_id'])
            lines = [{'line_id': l['line_id'], 'text': l['text']} for l in lines]
            status = body.get('status', 'draft')
            if status not in ('draft', 'in_review', 'needs_attention', 'reviewed'):
                raise ValueError('허용되지 않은 상태(동결/승인 불가)')
            if status == 'reviewed' and (not body.get('human_confirmed') or not lines):
                raise ValueError('사람의 명시적 전사·누락 확인이 필요합니다')
            if not isinstance(body.get('note', ''), str):
                raise ValueError('메모 형식 오류')
            d['history'].append({'at': now(), 'by': actor, 'before': {'lines': d['lines'], 'status': d['status'], 'note': d['note']},
                                 'after': {'lines': lines, 'status': status, 'note': body.get('note', '')}})
            d.update(lines=lines, status=status, note=body.get('note', ''), revision=d['revision'] + 1)
            if hasattr(self, 'finalize_saved_revision'):
                self.finalize_saved_revision(d, now, actor)
            self.drafts.mkdir(parents=True, exist_ok=True)
            tmp = self.path(tid).with_suffix('.tmp')
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            tmp.replace(self.path(tid))
            return d

    def listing(self):
        items = []
        for tid, r in self.rows.items():
            d = self.load(tid)
            items.append({'id': tid, 'group': r['source_id'].split('_')[0], 'source': r['source_id'],
                          'status': d['status'], 'lines': len(d['lines']), 'read_only': d['read_only'],
                          'ai_status': ai_review.ai_view(self, tid)['status']})
        def count(xs):
            return {'total': len(xs), 'frozen_v1': sum(x['read_only'] for x in xs),
                    'reviewed': sum(x['status'] == 'reviewed' for x in xs),
                    'draft': sum(x['status'] not in ('empty', 'reviewed', 'frozen_v1') for x in xs)}
        return {'items': items, 'progress': {'all': count(items), **{g: count([x for x in items if x['group'] == g]) for g in sorted({x['group'] for x in items})}}}


def handler_class(base, workspace, here, now):
    class Handler(base):
        def do_GET(self):
            from urllib.parse import unquote
            path = unquote(self.path.split('?')[0])
            try:
                if path == '/':
                    return self._send(200, (here / 'text-review.html').read_bytes(), 'text/html; charset=utf-8')
                if path == '/api/list':
                    return self._json(200, workspace.listing())
                if path == '/api/plan':
                    return self._json(200, ai_review.plan_view(workspace))
                if path.startswith('/api/locations/'):
                    tid = path[len('/api/locations/'):]
                    row = workspace.rows[tid]
                    hints = workspace.drafts / '_locations' / (tid + '.json')
                    lines = []
                    if hints.is_file():
                        data = json.loads(hints.read_text(encoding='utf-8'))
                        if data['id'] == tid and data['sha256'] == row['sha256']:
                            lines = data['lines']
                    return self._json(200, {'lines': lines})
                if path.startswith('/api/ai/'):
                    return self._json(200, ai_review.ai_view(workspace, path[len('/api/ai/'):]))
                if path.startswith('/api/truth/'):
                    return self._json(200, workspace.load(path[len('/api/truth/'):]))
                if path.startswith('/image/'):
                    r = workspace.rows[path[len('/image/'):]]
                    workspace.validate_image(r)
                    return self._send(200, Path(r['path']).read_bytes(), 'image/png')
                return self._json(404, {'error': 'not found'})
            except KeyError:
                return self._json(404, {'error': 'unknown id'})
            except (ValueError, OSError) as e:
                return self._json(400, {'error': str(e)})

        def do_POST(self):
            # JSON만 받고 외부 웹페이지의 교차 출처 저장을 거부한다.
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self._json(403, {'error': '외부 출처 저장 금지'})
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self._json(415, {'error': 'JSON required'})
            if not (self.path.startswith('/api/truth/') or self.path.startswith('/api/adopt/') or self.path in ('/api/plan', '/api/human')):
                return self._json(404, {'error': 'not found'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2_000_000:
                    raise ValueError('요청 크기 오류')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('JSON object required')
                if self.path == '/api/plan':
                    return self._json(200, ai_review.make_plan(workspace))
                if self.path == '/api/human':
                    return self._json(200, ai_review.save_human(workspace, body))
                if self.path.startswith('/api/adopt/'):
                    return self._json(200, ai_review.adopt(workspace, self.path[len('/api/adopt/'):], body['run_id'], body['revision']))
                d = workspace.save(self.path[len('/api/truth/'):], body, now)
                return self._json(200, d)
            except PermissionError as e:
                return self._json(403, {'error': str(e)})
            except (ValueError, KeyError, TypeError, OSError) as e:
                return self._json(400, {'error': str(e)})
    return Handler
