"""템플릿별 배치 계산.

치수 파일(spec/templates_spec.json)의 상자를 그대로 쓰고, 반복 칸은 영역을 개수로 나눈다.
좌우 반전(mirror)은 x → 970 − x − 너비 로 바꾼다.

각 build_XX 함수는 템플릿 HTML 에 넘길 값을 돌려준다.
  B     : 이름 → 상자(Box)
  A     : 이름 → 치수 파일 구역 이름 (위치 검사용 data-area)
  items : 반복 칸 목록 (칸마다 상자들)
  notes : 치수 파일과 다르게 처리한 점 (보고서에 남김)
"""
import math
from dataclasses import dataclass

from .spec import W

# 글자 줄 높이 비율 (글자 크기 × 비율 = 한 줄 높이)
LH_TITLE = 1.2
LH_BODY = 1.45
LH_SMALL = 1.35


def _f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def css(self):
        return f"left:{_f(self.x)}px;top:{_f(self.y)}px;width:{_f(self.w)}px;height:{_f(self.h)}px"

    def flip(self):
        return Box(W - self.x - self.w, self.y, self.w, self.h)

    def as_dict(self):
        return {"x": round(self.x, 2), "y": round(self.y, 2), "w": round(self.w, 2), "h": round(self.h, 2)}


class Layout:
    """좌우 반전 방식
    - 오른쪽에 사진 구역이 있는 템플릿: 사진 구역·사진은 반대편으로 옮기고(flip),
      글 쪽은 통째로 사진 구역 너비만큼 오른쪽으로 민다(translate).
      그래서 체크 원이 글 왼쪽에 있는 것처럼 글 묶음 안의 순서는 그대로 유지된다.
    - 사진 구역이 없는 템플릿(1B, 가운데 정렬형): 좌표를 그대로 뒤집는다.
    """

    def __init__(self, spec_items, mirror):
        self.items = spec_items
        self.by_name = {e["name"]: e for e in spec_items}
        self.mirror = mirror
        self.B, self.A, self.notes = {}, {}, []
        zone = next((e for e in spec_items if e["kind"] == "zone" and "사진" in e["name"]), None)
        if zone and zone["x"] > 0:
            self.photo = {zone["name"], "Add Image"}
            self.dx = zone["w"]
        else:
            self.photo, self.dx = None, 0

    def m(self, b, photo=False):
        if not self.mirror:
            return b
        if self.photo is None or photo:
            return b.flip()
        return Box(b.x + self.dx, b.y, b.w, b.h)

    def raw(self, name):
        e = self.by_name[name]
        return Box(e["x"], e["y"], e["w"], e["h"])

    def _is_photo(self, name):
        return bool(self.photo) and name in self.photo

    def take(self, key, name):
        """치수 파일 상자 하나를 그대로 쓴다."""
        self.B[key] = self.m(self.raw(name), photo=self._is_photo(name))
        self.A[key] = name
        return self.raw(name)

    def spec_boxes(self):
        """구역 선 보기용 (반전 반영)."""
        out = []
        for e in self.items:
            b = self.m(Box(e["x"], e["y"], e["w"], e["h"]), photo=self._is_photo(e["name"]))
            out.append(dict(b.as_dict(), name=e["name"], kind=e["kind"]))
        return out

    def ctx(self, **kw):
        d = {"B": self.B, "A": self.A, "notes": self.notes}
        d.update(kw)
        return d


def lines_in(h, size, lh):
    return max(1, int((h + 0.5) // (size * lh)))


# ---------------------------------------------------------------- S1 글·사진 좌우형
def build_S1(L, c, tid):
    """1A · 3A : 라벨 → 제목 → 한 줄 설명 → 본문, 오른쪽 사진."""
    label = {"1A": "브랜드명 (추가 요소)", "3A": "\"KEY INGREDIENT\" (추가 요소)"}[tid]
    L.take("zone", "사진 구역 (배경)")
    L.take("image", "Add Image")
    lab = L.take("label", label)
    head = L.take("headline", "Headline - Main")
    sub = L.take("sub", "한 줄 설명 (추가 요소)")
    body = L.take("body", "Body Text - Main")
    fs = {"label": 12, "headline": 32, "sub": 18, "body": 16}
    # 글 칸 묶음: 라벨 위쪽(y 60) ~ 본문 아래 끝(y 248). 칸 사이 간격은 치수 파일 좌표에서 계산
    col = Box(lab.x, lab.y, body.w, body.y + body.h - lab.y)
    flow = {
        "box": L.m(col), "limit": col.h,
        "label_h": lab.h, "head_h": head.h, "sub_h": sub.h,
        "gap1": head.y - (lab.y + lab.h),
        "gap2": sub.y - (head.y + head.h),
        "gap3": body.y - (sub.y + sub.h),
    }
    L.notes.append("글 칸을 위에서부터 쌓음(아래 글 밀기): 제목 1줄이면 치수 좌표 그대로, "
                   f"2줄이면 아래 글이 내려가고 전체가 y {col.y + col.h:g} 안에 들어가는지 검사")
    return L.ctx(fs=fs, flow=flow)


# ---------------------------------------------------------------- 2A
def build_2A(L, c, tid):
    L.take("zone", "제품 사진 구역")
    L.take("image", "Add Image")
    L.take("headline", "Headline - Main")
    L.take("body", "Body Text - Main")
    area = L.take("bullets_area", "Bullet Point 1~3(4)")
    icons = L.take("checks_area", "체크 아이콘 (추가 요소)")
    if c["layout"] == "single":
        # 대표 효능 하나: 글 쪽(48~548)에 제목 32px 2줄 + 문단 16px 4줄, 세로 가운데 쯤
        L.notes.append("배치 변형 대표 하나: 효능을 번호·불릿 없이 하나만 크게 (제목 32px 2줄 + 본문 16px 4줄)")
        return L.ctx(fs={"headline": 32, "body": 16}, items=[],
                     single_head=L.m(Box(48, 58, 500, 80)), single_body=L.m(Box(48, 150, 500, 96)))
    n = len(c["items"])
    row_h = area.h / n
    fs = {"headline": 30, "body": 15, "bullet": 17}
    items = []
    for i, it in enumerate(c["items"]):
        top = area.y + i * row_h
        items.append({
            "text": L.m(Box(area.x, top, area.w, row_h)),
            "icon": L.m(Box(icons.x + 1, top + (row_h - 28) / 2, 28, 28)),
            "lines": lines_in(row_h, fs["bullet"], 1.3),
            **it,
        })
    return L.ctx(fs=fs, items=items)


# ---------------------------------------------------------------- 공통 도우미
def columns(L, area_name, n):
    """가로 반복 칸: (반전된) 영역을 n 등분한 [(x, 칸너비), ...] — 왼쪽→오른쪽 순서 유지."""
    a = L.m(L.raw(area_name), photo=L._is_photo(area_name))
    cw = a.w / n
    return [(a.x + i * cw, cw) for i in range(n)]


def module_title(L, name):
    L.take("title", name)


# ---------------------------------------------------------------- 1B
def build_1B(L, c, tid):
    L.take("image", "Add Image - 970×300")
    L.take("panel", "글 얹기 상자")
    L.take("headline", "Headline - Main")
    L.take("body", "Body Text - Main")
    return L.ctx(fs={"headline": 36, "body": 16})


# ---------------------------------------------------------------- 2B
def build_2B(L, c, tid):
    L.take("zone", "사진 구역")
    L.take("image", "Add Image")
    icons = L.take("icons_area", "아이콘 1~3 (추가 요소)")
    area = L.take("text_area", "Subheadline - 1~3")
    fs = {"title": 20, "body": 15}
    th, bh, gap = fs["title"] * LH_TITLE, fs["body"] * LH_SMALL, 4
    n = len(c["items"])
    row_h = area.h / n
    zig = c["layout"] == "zigzag"
    if zig:
        L.notes.append("배치 변형 지그재그: 홀수 번째 줄(2·4번째)을 48px 들여씀 — 글 칸 너비도 48px 줄어 넘침 검사를 그 크기로 함")
    items = []
    for i, it in enumerate(c["items"]):
        top = area.y + i * row_h
        gh = th + (gap + bh if it["body"] else 0)
        gy = top + (row_h - gh) / 2
        ind = 48 if zig and i % 2 == 1 else 0
        items.append({
            "icon": L.m(Box(icons.x + ind, top + (row_h - 52) / 2, 52, 52)),
            "tbox": L.m(Box(area.x + ind, gy, area.w - ind, th)),
            "bbox": L.m(Box(area.x + ind, gy + th + gap, area.w - ind, bh)),
            "shifted": bool(ind),
            **it,
        })
    return L.ctx(fs=fs, items=items)


def card_box(x, cw, top, bottom):
    """반복 칸을 감싸는 연한 카드(모서리 12px). 칸 안쪽 4px 여백."""
    return Box(x + 4, top, cw - 8, bottom - top)


# ---------------------------------------------------------------- 3B
def build_3B(L, c, tid):
    L.take("zone", "제품 사진 구역")
    L.take("image", "Add Image")
    L.take("headline", "Headline Text - Module")
    A_img, A_t, A_b = "Add Image 1~3 (아이콘)", "Headline Text - 1~3", "Body Text - 1~3"
    L.A.update(icons=A_img, titles=A_t, bodies=A_b)
    yi, yt, yb = L.raw(A_img).y, L.raw(A_t).y, L.raw(A_b).y
    hb = L.raw(A_b).h
    items = []
    for (x, cw), it in zip(columns(L, A_img, len(c["items"])), c["items"]):
        items.append({
            "icon": Box(x + (cw - 80) / 2, yi, 80, 80),
            "tbox": Box(x + 6, yt, cw - 12, L.raw(A_t).h),
            "bbox": Box(x + 6, yb, cw - 12, hb),
            "card": card_box(x, cw, yi - 10, yb + hb + 8),
            **it,
        })
    return L.ctx(fs={"headline": 30, "title": 16, "body": 13}, items=items)


# ---------------------------------------------------------------- 4A
def build_4A(L, c, tid):
    L.take("zone", "사진 구역")
    L.take("image", "Add Image")
    L.take("mark", "인증 마크 (추가 요소)")
    L.take("headline", "Headline - Main")
    L.take("body", "Body Text - Main")
    return L.ctx(fs={"headline": 30, "body": 16})


# ---------------------------------------------------------------- 4B
def build_4B(L, c, tid):
    module_title(L, "모듈 제목 (추가 요소)")
    fs = {"title": 30, "item": 16, "label": 13}
    items = []
    if c["mode"] == "before_after":
        name = "전후일 때"
        a = L.m(L.raw(name))
        L.A["ba"] = name
        for x, it in zip((a.x, a.x + a.w - 400), c["items"]):
            items.append({
                "img": Box(x, a.y, 400, a.h),
                "lbox": Box(x + 12, a.y + a.h - 12 - 28, 376, 28),
                **it,
            })
        L.notes.append("전후 모드: 이미지 2칸 400×172(왼쪽 전, 오른쪽 후), 라벨은 이미지 왼쪽 아래에 얹음")
        return L.ctx(fs=fs, items=items)
    A_img, A_t = "Add Image 1~4", "Headline - 1~4"
    L.A.update(images=A_img, titles=A_t)
    ri, rt = L.raw(A_img), L.raw(A_t)
    for (x, cw), it in zip(columns(L, A_img, len(c["items"])), c["items"]):
        items.append({
            "img": Box(x + (cw - 120) / 2, ri.y, 120, 120),
            "tbox": Box(x + 8, rt.y, cw - 16, rt.h),
            "card": card_box(x, cw, ri.y - 10, rt.y + rt.h + 6),
            **it,
        })
    return L.ctx(fs=fs, items=items)


# ---------------------------------------------------------------- 5A
def build_5A(L, c, tid):
    L.take("headline", "Headline - Main (선택)")
    A_sw, A_cap = "Add Image 1~n (스와치)", "Caption 1~n"
    sw, cap = L.m(L.raw(A_sw)), L.m(L.raw(A_cap))
    n = len(c["items"])
    ch = 14 * 1.3
    items = []
    if n <= 6:
        d = 96
        L.A.update(swatches=A_sw, captions=A_cap)
        cw = sw.w / n
        for i, it in enumerate(c["items"]):
            x = sw.x + i * cw
            items.append({"sw": Box(x + (cw - d) / 2, sw.y + sw.h - d, d, d),
                          "cbox": Box(x + 4, cap.y, cw - 8, ch), **it})
        L.notes.append("스와치 6개 이하: 지름 96 한 줄, 원은 스와치 구역 아래쪽에 붙여 색상명과 가깝게 둠")
    else:
        d = 64
        L.A.update(swatches="", captions="")
        per = math.ceil(n / 2)
        cw = sw.w / per
        rows = [c["items"][:per], c["items"][per:]]
        ys = [sw.y, cap.y - 4 - d]          # 둘째 줄 색상명이 색상명 구역(y 228)에 오도록
        for r, (row, y) in enumerate(zip(rows, ys)):
            x0 = sw.x + (sw.w - len(row) * cw) / 2  # 둘째 줄이 짧으면 가운데로
            for i, it in enumerate(row):
                x = x0 + i * cw
                items.append({"sw": Box(x + (cw - d) / 2, y, d, d),
                              "cbox": Box(x + 4, y + d + 4, cw - 8, ch), **it})
        L.notes.append("스와치 7~12개: 지름 64 두 줄. 색상명 칸이 한 줄뿐이라 색상명을 스와치마다 바로 아래에 둠 "
                       f"(첫 줄 색상명은 스와치 구역 안, 둘째 줄 원은 스와치 구역 아래로 {ys[1] + d - (sw.y + sw.h):g}px 나감)")
    return L.ctx(fs={"headline": 24, "caption": 14}, items=items)


# ---------------------------------------------------------------- 5B
def build_5B(L, c, tid):
    module_title(L, "모듈 제목 (추가 요소)")
    A_img, A_t, A_b = "Add Image 1~4", "Headline - 1~4", "Body Text - 1~4"
    L.A.update(images=A_img, titles=A_t, bodies=A_b)
    ri, rt, rb = L.raw(A_img), L.raw(A_t), L.raw(A_b)
    items = []
    for (x, cw), it in zip(columns(L, A_img, len(c["items"])), c["items"]):
        items.append({
            "img": Box(x + (cw - 140) / 2, ri.y, 140, 140),
            "tbox": Box(x + 6, rt.y, cw - 12, rt.h),
            "bbox": Box(x + 6, rb.y, cw - 12, rb.h),
            "card": card_box(x, cw, ri.y - 8, rb.y + rb.h + 6),
            **it,
        })
    return L.ctx(fs={"title": 30, "item": 16, "body": 13}, items=items)


# ---------------------------------------------------------------- 6A
def build_6A(L, c, tid):
    L.take("title", "Headline Text - Module")
    A_img, A_t, A_b = "Add Image 1~3", "Headline Text - 1~3", "Body Text - 1~3"
    A_num = "번호 원·화살표 (추가 요소)"
    L.A.update(images=A_img, titles=A_t, bodies=A_b, nums=A_num)
    ri, rt, rb = L.raw(A_img), L.raw(A_t), L.raw(A_b)
    cols = columns(L, A_img, len(c["items"]))
    lay = c["layout"]
    items = []
    if lay == "timeline":
        # 가로 선 위 번호 점, 아래 단계 이름·설명. 단계 사진은 쓰지 않는다.
        ly = 150
        L.notes.append("배치 변형 타임라인: 가로 선(y 150) 위 번호 점, 아래 단계 이름(y 180)·설명(y 204). 단계 사진은 그리지 않음")
        line = Box(cols[0][0] + cols[0][1] / 2, ly - 1, (cols[-1][0] + cols[-1][1] / 2) - (cols[0][0] + cols[0][1] / 2), 2)
        for (x, cw), it in zip(cols, c["items"]):
            cx = x + cw / 2
            items.append({"img": None, "num": Box(cx - 18, ly - 18, 36, 36), "arrow": None, "seg": None,
                          "tbox": Box(x + 6, 180, cw - 12, rt.h), "bbox": Box(x + 6, 204, cw - 12, rb.h),
                          "shifted": True, **it})
        return L.ctx(fs={"title": 30, "item": 16, "body": 13}, items=items, tl_line=line)
    zig = lay == "zigzag"
    if zig:
        L.notes.append("배치 변형 지그재그: 단계 칸을 위아래 번갈아 ±20px, 연결선이 꺾이며 이어짐")
    dys = [(-20 if i % 2 == 0 else 20) if zig else 0 for i in range(len(cols))]
    for i, ((x, cw), it) in enumerate(zip(cols, c["items"])):
        ix = x + (cw - 120) / 2
        dy = dys[i]
        arrow = seg = None
        if i < len(cols) - 1:
            bx = x + cw
            arrow = Box(bx - 11, ri.y + 60 - 11 + (dy + dys[i + 1]) / 2, 22, 22)
            # 연결선: 이 칸 사진 오른쪽 끝 → 다음 칸 사진 왼쪽 끝 (사진 가운데 높이)
            nx = cols[i + 1][0] + (cols[i + 1][1] - 120) / 2
            seg = {"x1": ix + 120 + 10, "y1": ri.y + 60 + dy, "x2": nx - 10, "y2": ri.y + 60 + dys[i + 1]}
        items.append({
            "img": Box(ix, ri.y + dy, 120, 120),
            "num": Box(ix, ri.y + dy, 30, 30),
            "arrow": arrow, "seg": seg,
            "tbox": Box(x + 6, rt.y + dy, cw - 12, rt.h),
            "bbox": Box(x + 6, rb.y + dy, cw - 12, rb.h),
            "shifted": bool(dy),
            **it,
        })
    return L.ctx(fs={"title": 30, "item": 16, "body": 13}, items=items, tl_line=None)


# ---------------------------------------------------------------- 6B
def build_6B(L, c, tid):
    L.take("zone", "사진 구역")
    L.take("image", "Add Image")
    L.take("headline", "Headline - Main")
    nums = L.take("nums_area", "번호 원 (추가 요소)")
    area = L.take("text_area", "Subheadline·Body Text - Main/2/3")
    fs = {"headline": 30, "step": 17, "body": 14}
    if c["layout"] == "prose":
        # 줄글형: 번호·단계 없이 문단 하나. 번호 칸 + 단계 칸을 합친 자리(48~552, y 104~280)에 본문 16px
        L.notes.append("배치 변형 줄글형: 사용법이 줄글이라 단계 번호 없이 문단으로 (본문 16px, 최대 7줄)")
        box = L.m(Box(nums.x, area.y, area.x + area.w - nums.x, area.h))
        return L.ctx(fs=dict(fs, prose=16), items=[], tl_line=None, prose_box=box, prose_lines=7)
    th, bl, gap = fs["step"] * LH_TITLE, fs["body"] * LH_SMALL, 2
    n = len(c["items"])
    row_h = area.h / n
    body_lines = max(1, int((row_h - th - gap + 0.5) // bl))
    items = []
    for i, it in enumerate(c["items"]):
        top = area.y + i * row_h
        gh = th + (gap + body_lines * bl if it["body"] else 0)
        gy = top + max(0, (row_h - gh) / 2)
        items.append({
            "num": L.m(Box(nums.x, gy + (th - 30) / 2, 30, 30)),
            "tbox": L.m(Box(area.x, gy, area.w, th)),
            "bbox": L.m(Box(area.x, gy + th + gap, area.w, body_lines * bl)),
            "body_lines": body_lines,
            **it,
        })
    vline = None
    if c["layout"] == "timeline" and items:
        a, b = items[0]["num"], items[-1]["num"]
        vline = Box(a.x + a.w / 2 - 1, a.y + a.h / 2, 2, (b.y + b.h / 2) - (a.y + a.h / 2))
        L.notes.append("배치 변형 세로 타임라인: 번호 점을 세로 선으로 이음")
    return L.ctx(fs=fs, items=items, tl_line=vline)


# ---------------------------------------------------------------- 7A
def build_7A(L, c, tid):
    L.take("zone", "사진 구역")
    L.take("image", "Add Image")
    L.take("label", "\"OUR STORY\" (추가 요소)")
    L.take("headline", "Headline - Main")
    L.take("body", "Body Text - Main")
    return L.ctx(fs={"label": 12, "headline": 32, "body": 15})


# ---------------------------------------------------------------- 7B
def build_7B(L, c, tid):
    module_title(L, "모듈 제목 (추가 요소)")
    A_img, A_t, A_b = "Add Image 1~4 (아이콘)", "Headline - 1~4", "Body Text - 1~4"
    L.A.update(images=A_img, titles=A_t, bodies=A_b)
    ri, rt, rb = L.raw(A_img), L.raw(A_t), L.raw(A_b)
    items = []
    for (x, cw), it in zip(columns(L, A_img, len(c["items"])), c["items"]):
        items.append({
            "img": Box(x + (cw - 80) / 2, ri.y, 80, 80),
            "tbox": Box(x + 8, rt.y, cw - 16, rt.h),
            "bbox": Box(x + 8, rb.y, cw - 16, rb.h),
            "card": card_box(x, cw, ri.y - 10, rb.y + rb.h + 8),
            **it,
        })
    return L.ctx(fs={"title": 30, "item": 16, "body": 13}, items=items)


BUILDERS = {
    "1A": build_S1, "1B": build_1B,
    "2A": build_2A, "2B": build_2B,
    "3A": build_S1, "3B": build_3B,
    "4A": build_4A, "4B": build_4B,
    "5A": build_5A, "5B": build_5B,
    "6A": build_6A, "6B": build_6B,
    "7A": build_7A, "7B": build_7B,
}


def build(spec, content):
    tid = content["template"]
    if tid not in BUILDERS:
        raise NotImplementedError(f"{tid} 템플릿은 아직 만들지 않음")
    L = Layout(spec[tid], content["mirror"])
    ctx = BUILDERS[tid](L, content, tid)
    ctx.setdefault("tl_line", None)
    st, lay = content["style"], content["layout"]
    if lay in ("vcenter", "center"):
        L.notes.append(f"배치 변형 {'세로 가운데: 글 덩어리를 세로 가운데로' if lay == 'vcenter' else '가운데 정렬: 글을 가운데 정렬'}")
    if st["card"]:
        L.notes.append("장식 카드: 반복 칸을 연한 배경 카드(모서리 12px)로 감쌈")
    if tid == "1B" and (st["photo_shape"] != "square" or st["photo_backdrop"] != "none" or st["photo_border"] != "none"):
        L.notes.append("1B 는 사진이 화면 전체라 사진 모양·뒤 배경 도형·테두리를 적용하지 않음")
    ctx["notes"] = L.notes + content.get("style_notes", [])
    ctx["spec_boxes"] = L.spec_boxes()
    ctx["spec_map"] = {b["name"]: b for b in ctx["spec_boxes"]}
    return ctx
