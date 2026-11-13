"""OCR v5 후보 A. 순수 함수만 제공하며 파일·제품·GT를 수정하지 않는다."""
from fractions import Fraction
from functools import lru_cache
from .ocr import text_sequence, edits


@lru_cache(maxsize=32768)
def distance(a, b):
    """Myers bit-vector Levenshtein; 선택 대응의 S/D/I는 v4로 역추적한다."""
    if not a:
        return len(b)
    masks = {}
    for i, c in enumerate(a):
        masks[c] = masks.get(c, 0) | (1 << i)
    pv, mv, score, high = ~0, 0, len(a), 1 << (len(a)-1)
    for c in b:
        eq = masks.get(c, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph, mh = mv | ~(xh | pv), pv & xh
        score += bool(ph & high) - bool(mh & high)
        ph, mh = (ph << 1) | 1, mh << 1
        pv, mv = mh | ~(xv | ph), ph & xv
    return score


def assignment(cost):
    """정방 정수 비용 Hungarian. 인덱스 오름차순 동률 처리."""
    n = len(cost)
    u, v, p, way = [0]*(n+1), [0]*(n+1), [0]*(n+1), [0]*(n+1)
    for i in range(1, n+1):
        p[0], j0 = i, 0
        best, used = [float('inf')]*(n+1), [False]*(n+1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], float('inf'), 0
            for j in range(1, n+1):
                if not used[j]:
                    cur = cost[i0-1][j-1] - u[i0] - v[j]
                    if cur < best[j]:
                        best[j], way[j] = cur, j0
                    if best[j] < delta:
                        delta, j1 = best[j], j
            for j in range(n+1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    best[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0], j0 = p[j1], j1
    return sorted((p[j]-1, j-1) for j in range(1, n+1))


def groups(lines):
    for i in range(len(lines)):
        for count in range(1, min(3, len(lines)-i)+1):
            yield tuple(range(i, i+count)), ''.join(lines[i:i+count])


def score_lines(gt, output):
    if not isinstance(gt, list) or not isinstance(output, list) or any(not isinstance(x, str) for x in gt+output):
        raise ValueError('줄은 문자열 배열이어야 함')
    gg, pp = [text_sequence([x]) for x in gt], [text_sequence([x]) for x in output]
    gi, pi = [i for i,x in enumerate(gg) if x], [i for i,x in enumerate(pp) if x]
    g, q = [gg[i] for i in gi], [pp[i] for i in pi]
    n, m = len(g), len(q)
    cost = [[distance(g[i], q[j]) if i<n and j<m else len(g[i]) if i<n else len(q[j]) if j<m else 0
             for j in range(n+m)] for i in range(n+m)]
    single = [((i,), (j,)) for i,j in assignment(cost) if i<n and j<m]
    candidates = []
    for a, x in groups(g):
        for b, y in groups(q):
            d = distance(x,y)
            saving = len(x)+len(y)-d
            if saving > 0:
                candidates.append((Fraction(d,max(len(x),len(y))), -saving, len(a)+len(b), a,b))
    used_g, used_q, grouped = set(), set(), []
    for _,_,_,a,b in sorted(candidates):
        if not used_g.intersection(a) and not used_q.intersection(b):
            grouped.append((a,b)); used_g.update(a); used_q.update(b)

    def total(pairs):
        ag={i for a,_ in pairs for i in a}; aq={j for _,b in pairs for j in b}
        return (sum(distance(''.join(g[i] for i in a), ''.join(q[j] for j in b)) for a,b in pairs)
                +sum(len(x) for i,x in enumerate(g) if i not in ag)+sum(len(x) for j,x in enumerate(q) if j not in aq))
    # 줄 경계만 다른 완전히 같은 문자열에는 오류를 부과하지 않는다.
    ordered = [(tuple(range(n)), tuple(range(m)))] if n or m else []
    paths = [('singleton', single), ('grouped', grouped), ('ordered', ordered)]
    strategy, pairs = min(paths, key=lambda item: total(item[1]))
    used_g={i for a,_ in pairs for i in a}; used_q={j for _,b in pairs for j in b}
    pairs = pairs + [((i,),()) for i in range(n) if i not in used_g] + [((),(j,)) for j in range(m) if j not in used_q]
    details=[]
    for a,b in pairs:
        x,y=''.join(g[i] for i in a), ''.join(q[j] for j in b)
        detail=edits(x,y)
        detail.update(gt_indices=[gi[i] for i in a],output_indices=[pi[j] for j in b])
        details.append(detail)
    result={k:sum(d[k] for d in details) for k in ('S','D','I','N')}
    result.update(output_characters=sum(map(len,q)),strategy=strategy,matches=details)
    result['errors']=result['S']+result['D']+result['I']
    result['cer_percent']=100*result['errors']/result['N'] if result['N'] else None
    assert result['N']==sum(map(len,g))
    assert result['output_characters']-result['N']==result['I']-result['D']
    assert result['errors']==min(total(p) for _,p in paths)
    return result
