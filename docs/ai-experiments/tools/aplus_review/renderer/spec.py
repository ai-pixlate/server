"""템플릿 정보: 치수 파일 읽기, 칸 이름, 입력 내용 검사."""
import json
import re
from pathlib import Path

from .icons import NAMES as ICON_NAMES, svg as icon_svg

ROOT = Path(__file__).resolve().parent.parent
# 원본: Claude outputs/_stage4/A+템플릿14개_세부치수_초안.json (2026-11-10 복사)
SPEC_PATH = ROOT / "spec" / "templates_spec.json"
W, H = 970, 300

META = {
    "1A": ("1 제품 소개", "S1 글·사진 좌우형", "Single Left/Right Image"),
    "1B": ("1 제품 소개", "S3 통짜 사진형", "자체 배치"),
    "2A": ("2 효능", "S4 글·나열+사진형", "Single Image & Highlights"),
    "2B": ("2 효능", "S6 사진+나열형", "Single Image & Specs Detail"),
    "3A": ("3 성분", "S1 글·사진 좌우형", "Single Left/Right Image"),
    "3B": ("3 성분", "S4 글·나열+사진형", "자체 배치"),
    "4A": ("4 신뢰·증명", "S1 글·사진 좌우형", "Single Left/Right Image"),
    "4B": ("4 신뢰·증명", "S2 제목+나열형", "Four Images & Text"),
    "5A": ("5 옵션", "S5 통짜 나열형", "자체 배치"),
    "5B": ("5 라인업", "S2 제목+나열형", "Four Images & Text"),
    "6A": ("6 사용법", "S2 제목+나열형", "Three Images & Text"),
    "6B": ("6 사용법", "S4 글·나열+사진형", "Single Image & Highlights"),
    "7A": ("7 브랜드", "S1 글·사진 좌우형", "Single Left/Right Image"),
    "7B": ("7 브랜드", "S2 제목+나열형", "Four Images & Text"),
}

# 템플릿별 필수 칸 / 반복 칸 규칙 (개수 최소, 최대, 칸마다 필수 항목)
RULES = {
    "1A": {"required": ["headline", "image"]},
    "1B": {"required": ["headline", "image"]},
    "2A": {"required": ["headline", "image", "items"], "items": (2, 4, ["title"])},
    "2B": {"required": ["image", "items"], "items": (2, 4, ["title"])},
    "3A": {"required": ["headline", "image"]},
    "3B": {"required": ["headline", "image", "items"], "items": (2, 4, ["title"])},
    "4A": {"required": ["headline", "image"]},
    "4B": {"required": ["headline", "items"], "items": (2, 4, ["title", "image"])},
    "5A": {"required": ["items"], "items": (2, 12, ["color|image"])},
    "5B": {"required": ["headline", "items"], "items": (2, 4, ["title", "image"])},
    "6A": {"required": ["headline", "items"], "items": (2, 4, ["title"])},
    "6B": {"required": ["headline", "image", "items"], "items": (2, 4, ["title"])},
    "7A": {"required": ["headline", "image"]},
    "7B": {"required": ["headline", "items"], "items": (2, 4, ["title"])},
}

# ---- 2026-11-10 추가: 사진 처리·장식(style)과 안쪽 배치 변형(layout). 주지 않으면 지금과 같은 결과.
# 사용자 결정으로 빼고 기존대로 둔 것: 아이콘 틀, 경계 넘기기, 그림자, 2A 가로, 2B 2×2, 3B 세로, 4B 2×2,
#   5A 사진 포함, 5B 엇갈림, 7B 2×2 — 아래 목록에 없으므로 쓰면 오류.
STYLE_OPTIONS = {
    "photo_shape": ["square", "rounded", "circle", "arch"],          # 큰 사진 모양 (circle·arch 는 꽉 채워 자름)
    "photo_backdrop": ["none", "circle", "blob", "arch", "gradient"],  # 큰 사진 뒤 배경 도형
    "photo_border": ["none", "thin"],
    "item_image_shape": ["square", "rounded", "circle"],             # 반복 칸 안 작은 이미지
    "headline_underline": ["none", "bar", "line"],                   # bar = 40×3 강조색
    "label": ["text", "pill"],                                       # 라벨 모양
    "step_number": ["filled", "outline", "square", "big"],           # 6A·6B
    "step_connector": ["arrow", "line", "dotted", "none"],           # 6A
    "margin_deco": ["none", "dots", "corner", "circle"],             # 빈 여백에만
    "card": [False, True],                                           # 3B·4B·5B·7B
    "icon_style": ["fill", "line"],                                  # 2B·3B·7B 뜻 아이콘(items[].icon): 채운 / 선 (11-12)
}
STYLE_DEFAULT = {k: v[0] for k, v in STYLE_OPTIONS.items()}
STYLE_ONLY_FOR = {"step_number": {"6A", "6B"}, "step_connector": {"6A"}, "card": {"3B", "4B", "5B", "7B"}, "icon_style": {"2B", "3B", "7B"},
                  "label": {"1A", "3A", "7A"}}
LAYOUTS = {
    "2A": ["default", "single"],   # single = 대표 효능 하나를 크게(제목 + 문단, 불릿 없음) 11-11 추가
    "2B": ["default", "zigzag"],
    "4B": ["default", "cards"],
    "5A": ["default", "chips"],
    "5B": ["default", "cards"],
    "6A": ["default", "zigzag", "timeline"],
    "6B": ["default", "timeline", "prose"],   # prose = 줄글형(제목 + 짧은 문단, 단계 번호 없음) 11-11 추가
    "7B": ["default", "cards"],
    "1A": ["default", "vcenter", "center"],
    "3A": ["default", "vcenter", "center"],
    "4A": ["default", "vcenter", "center"],
    "7A": ["default", "vcenter", "center"],
}
LAYOUT_KO = {"default": "기본", "zigzag": "지그재그", "cards": "카드형 가로", "chips": "칩", "timeline": "타임라인", "prose": "줄글형", "single": "대표 하나",
             "vcenter": "세로 가운데", "center": "가운데 정렬"}

DEFAULT_THEME = {"bg": "#FFFFFF", "photo_bg": "#EEF2F5", "text": "#222222", "accent": "#5B8C6A"}
# accent_text(선택, 11-12 배경색 작업): 강조색을 "글자"로 쓸 때의 색(라벨·부제·번호). 없으면 accent 그대로 — 강조색이 밝아
#   흰 바탕에서 안 읽힐 때(예: 노랑 #FFC800) 더 진한 색을 준다. 도형·선·아이콘은 accent 그대로.
DEFAULT_LABEL = {"3A": "KEY INGREDIENT", "7A": "OUR STORY"}
HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class ContentError(ValueError):
    """입력 JSON 이 템플릿 규칙에 맞지 않을 때."""


def load_spec(path=SPEC_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _empty(v):
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v)


def resolve_path(src, base_dir):
    p = Path(src)
    if p.is_absolute():
        cands = [p]
    else:
        cands = [Path(base_dir) / p, ROOT / p, Path.cwd() / p]
    for c in cands:
        if c.is_file():
            return c.resolve()
    raise ContentError(f"이미지 파일을 찾을 수 없음: {src}")


def norm_image(v, base_dir, default_fit):
    if _empty(v):
        return None
    if isinstance(v, str):
        v = {"src": v}
    fit = v.get("fit") or default_fit
    if fit not in ("contain", "cover"):
        raise ContentError(f"fit 은 contain 또는 cover 여야 함: {fit}")
    path = resolve_path(v["src"], base_dir)
    return {"src": v["src"], "path": str(path), "uri": path.as_uri(), "fit": fit}


def validate(content, base_dir="."):
    """검사 후 정리된 내용(dict)을 돌려준다. 문제가 있으면 ContentError."""
    errs = []
    tid = content.get("template")
    if tid not in RULES:
        raise ContentError(f"template 값이 잘못됨: {tid!r} (가능: {', '.join(RULES)})")
    rule = RULES[tid]
    mode = content.get("mode")
    if mode not in (None, "before_after"):
        errs.append(f"mode 값이 잘못됨: {mode!r}")
    if mode == "before_after" and tid != "4B":
        errs.append("mode=before_after 는 4B 에서만 쓸 수 있음")

    required = list(rule["required"])
    if tid == "2A" and content.get("layout") == "single":
        # 2A 대표 하나(11-11 추가): 효능이 2~4개 포인트로 정리돼 있지 않을 때 중요한 효능 하나를 크게. items 없음
        required = [k for k in required if k != "items"]
        if content.get("items"):
            errs.append("2A 대표 하나(single)에는 items 를 쓰지 않음 — 설명은 body 에")
    if tid == "6B" and content.get("layout") == "prose":
        # 6B 줄글형(2026-11-11 추가): 단계 목록 대신 제목 + 짧은 문단. items 대신 body 필수
        required = [k for k in required if k != "items"] + ["body"]
        if content.get("items"):
            errs.append("6B 줄글형(prose)에는 items 를 쓰지 않음 — 문단은 body 에")
    for k in required:
        if _empty(content.get(k)):
            errs.append(f"필수 칸 없음: {k}")

    theme = dict(DEFAULT_THEME)
    theme.update({k: v for k, v in (content.get("theme") or {}).items() if v})
    for k, v in theme.items():
        if not HEX.match(str(v)):
            errs.append(f"theme.{k} 색 코드가 잘못됨: {v} (예: #5B8C6A)")

    items = content.get("items") or []
    if "items" in rule and items:
        lo, hi, need = rule["items"]
        if mode == "before_after":
            lo, hi, need = 2, 2, ["title", "image"]
        if not (lo <= len(items) <= hi):
            errs.append(f"items 개수 {len(items)}개 — {tid}{' 전후 모드' if mode else ''}는 {lo}~{hi}개")
        for i, it in enumerate(items):
            for k in need:
                if not any(not _empty(it.get(x)) for x in k.split("|")):
                    errs.append(f"items[{i}] 에 {k.replace('|', ' 또는 ')} 없음")
            if it.get("icon") and it["icon"] not in ICON_NAMES:
                errs.append(f"items[{i}].icon '{it['icon']}' 은(는) 없는 아이콘 (renderer/icons.py 목록)")
            if it.get("color") and not HEX.match(str(it["color"])):
                errs.append(f"items[{i}].color 색 코드가 잘못됨: {it['color']}")
    # 배치 변형 — 표에 있는 값만
    layout = content.get("layout") or "default"
    allowed = LAYOUTS.get(tid, ["default"])
    if layout not in allowed:
        errs.append(f"layout '{layout}' 은(는) {tid} 에서 쓸 수 없음 (가능: {', '.join(allowed)})")
    if tid == "4B" and layout == "cards" and mode == "before_after":
        errs.append("4B 전후 모드에는 카드형을 쓸 수 없음")

    # 사진 처리·장식
    style = dict(STYLE_DEFAULT)
    style_notes = []
    raw_style = content.get("style") or {}
    if not isinstance(raw_style, dict):
        errs.append("style 은 {\"이름\": 값} 형식이어야 함")
        raw_style = {}
    for k, v in raw_style.items():
        if k not in STYLE_OPTIONS:
            errs.append(f"style.{k} 은(는) 없는 옵션 (가능: {', '.join(STYLE_OPTIONS)})")
            continue
        if v not in STYLE_OPTIONS[k]:
            errs.append(f"style.{k} 값 '{v}' 은(는) 쓸 수 없음 (가능: {', '.join(map(str, STYLE_OPTIONS[k]))})")
            continue
        if k in STYLE_ONLY_FOR and tid not in STYLE_ONLY_FOR[k] and v != STYLE_DEFAULT[k]:
            style_notes.append(f"style.{k} 는 {', '.join(sorted(STYLE_ONLY_FOR[k]))} 에만 적용 — {tid} 에서는 무시")
            continue
        style[k] = v
    if layout == "cards":
        style["card"] = True
    if errs:
        raise ContentError("\n".join(errs))

    main_fit = "cover" if tid == "1B" else "contain"
    item_fit = "cover" if (mode == "before_after" or tid == "5A") else "contain"
    out = {
        "template": tid,
        "mirror": bool(content.get("mirror")),
        "mode": mode,
        "theme": theme,
        "headline": content.get("headline") or "",
        "subheadline": content.get("subheadline") or "",
        "body": content.get("body") or "",
        "label": content.get("label") or DEFAULT_LABEL.get(tid, ""),
        "overlay": content.get("overlay", True),
        "round_images": bool(content.get("round_images")),
        "image": norm_image(content.get("image"), base_dir, main_fit),
        "mark": norm_image(content.get("mark"), base_dir, "contain"),
        "items": [],
        "layout": layout,
        "style": style,
        "style_notes": style_notes,
    }
    if out["image"] and style["photo_shape"] in ("circle", "arch"):
        out["image"]["fit"] = "cover"  # 원·아치는 꽉 채워 자름
    for it in items:
        out["items"].append({
            "title": it.get("title") or "",
            "body": it.get("body") or "",
            "color": it.get("color") or "",
            "image": norm_image(it.get("image"), base_dir, item_fit),
            # 뜻 아이콘(11-12): 사진이 없을 때만, 2B·3B·7B 에서만. 사진이 있으면 사진 우선
            "glyph": icon_svg(it["icon"], style["icon_style"]) if (it.get("icon") in ICON_NAMES and tid in ("2B", "3B", "7B")
                                                                   and not it.get("image")) else "",
        })
    return out
