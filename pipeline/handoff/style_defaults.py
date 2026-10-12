"""역할별 스타일 기본값 — 설정 자료 로더 · 폰트 자료 검증 · 적용값 계산(순수 함수) [통합 D9-3 · D11].

설정 자료 정본은 `pipeline/data/style_defaults.json`(AI 소유) 하나다. 이 모듈은 그 자료를 읽고 검증하며, 블록 하나의
조판 적용값과 출처를 계산한다. 조판 · 이미지 생성 · 넘침 측정은 하지 않는다 — 운영 렌더는 BE(`app/typeset.py`) 소유이고,
AI 쪽 참고 렌더는 `pipeline/handoff/style_reference.py`(검증용, 운영 렌더 아님)다.

- 자료 구조: `type_tokens`(디자인 이미지의 토큰 값) · `pipeline_mapping`(사용자 승인 역할 대응 · 보완값) · `fonts`(공식
  배포 파일의 출처 · 해시 · 굵기). 디자인이 명시한 값과 승인한 매핑을 구조로 분리해 출처를 섞지 않는다.
- 측정값 우선: ⑦의 유효한 font_color · est_font_px · align은 그대로 쓴다. NULL인 항목만 역할 기본값으로 채운다.
  ⑦ 실패(객체 NULL)는 세 항목 모두 기본값이고 단계 실패 상태는 그대로 둔다 — 측정 성공으로 바꾸지 않는다.
- 크기: 측정 est_font_px는 이미 원본 px다. 기준 폭 배율이나 em_ratio(1.35)를 다시 곱하지 않는다.
  기본 크기만 `base_px * 원본 폭 / reference_width_px`로 환산한다(곱한 뒤 한 번 나눈다, 반올림 없음).
- 폰트 · 굵기 · 자간 · 행간은 역할 설정(role_config)이다. 측정값처럼 기록하지 않는다.
  자간 px = (letter_spacing_pct / 100) × 적용 크기, 행간 px = line_height_ratio × 적용 크기.
- bg_color는 조판에 쓰지 않는다(사진 · 그라데이션 배경을 칠하지 않음). 배경색 기본값은 없다.
- 처리 제외(라벨 · 로고 · 규제 제외 등 is_excluded가 false가 아님) · 빈 원문 · 번역문 없음은 조판하지 않는다.
- 입력이 계약을 어기면(형식 밖 측정값, 단계 완료인데 대상 블록 style 없음 등) 기본값으로 덮지 않고 거부한다.

이 모듈의 예외 이름은 AI 쪽 검증 도구용이다. 운영 오류 코드 · API enum이 아니다.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pipeline.handoff.canonical import sha256_canonical, sha256_file

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
STYLE_DEFAULTS_PATH = DATA_DIR / "style_defaults.json"
ROLES = ("title", "body", "caption", "price", "caution")
ALIGNS = ("left", "center", "right")
# 환산 규칙 버전 — 위 docstring의 크기 · 자간 · 행간 계산과 측정값 우선 규칙. 규칙이 바뀌면 올리고 입력 지문에 넣는다
RULE_VERSION = "role-style-rule/1"
HEX_COLOR = r"^#[0-9A-F]{6}$"
SHA256 = r"^[0-9a-f]{64}$"


class StyleDefaultsError(ValueError):
    """설정 자료가 구조 · 대응 · 값 범위를 어긴다."""


class FontAssetError(ValueError):
    """폰트 파일이 없거나 해시 · 굵기 · 이름 메타데이터가 설정 자료와 다르다."""


class StyleInputInvalid(ValueError):
    """블록 입력이 계약(측정값 형식 · 단계 상태와 style의 관계)을 어긴다. 기본값으로 덮지 않는다."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Approval(_Model):
    decision: str = Field(min_length=1)
    approved_by: Literal["user"]
    approved_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    pm_reply_recorded: bool = Field(strict=True)


class TypeToken(_Model):
    size_px: float = Field(gt=0)
    weight: str = Field(min_length=1)
    letter_spacing_pct: float

    @field_validator("size_px", "letter_spacing_pct")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("유한한 수여야 한다")
        return v


class TypeTokens(_Model):
    source: Literal["design_image"]
    note: str
    tokens: dict[str, TypeToken] = Field(min_length=1)


class PipelineMapping(_Model):
    source: Literal["user_approved"]
    note: str
    font_family: str = Field(min_length=1)
    reference_width_px: int = Field(gt=0, strict=True)
    font_color: str = Field(pattern=HEX_COLOR)
    align: Literal["left", "center", "right"]
    line_height_ratio: float = Field(gt=0)
    roles: dict[str, str]

    @field_validator("roles")
    @classmethod
    def _five_roles(cls, v: dict[str, str]) -> dict[str, str]:
        if sorted(v) != sorted(ROLES):
            raise ValueError(f"역할은 {ROLES} 5종이어야 한다: {sorted(v)}")
        return v


def _rel_path(v: str) -> str:
    p = PurePosixPath(v)
    if p.is_absolute() or ".." in p.parts or "\\" in v or ":" in v:  # OS와 무관하게 / 시작 · 드라이브 문자도 거부
        raise ValueError(f"data 폴더 기준 상대 경로(/ 구분)여야 한다: {v!r}")
    return v


class FontPackage(_Model):
    name: str
    release: str
    url: str = Field(pattern=r"^https://")
    zip_bytes: int = Field(gt=0, strict=True)
    zip_sha256: str = Field(pattern=SHA256)
    license: str
    license_member: str
    license_file: str
    license_sha256: str = Field(pattern=SHA256)

    @field_validator("license_file")
    @classmethod
    def _rel(cls, v: str) -> str:
        return _rel_path(v)


class FontFace(_Model):
    weight: int = Field(ge=1, le=1000, strict=True)
    zip_member: str
    file: str
    bytes: int = Field(gt=0, strict=True)
    sha256: str = Field(pattern=SHA256)
    postscript_name: str
    version: str

    @field_validator("file")
    @classmethod
    def _rel(cls, v: str) -> str:
        return _rel_path(v)


class Fonts(_Model):
    package: FontPackage
    faces: dict[str, FontFace] = Field(min_length=1)


class StyleDefaultsFile(_Model):
    schema_version: Literal["1"]
    defaults_version: str = Field(pattern=r"^role-style@\d{4}-\d{2}-\d{2}\.\d+$")
    approval: Approval
    type_tokens: TypeTokens
    pipeline_mapping: PipelineMapping
    fonts: Fonts

    @model_validator(mode="after")
    def _links(self) -> "StyleDefaultsFile":
        tokens = self.type_tokens.tokens
        for role, token in self.pipeline_mapping.roles.items():
            if token not in tokens:
                raise ValueError(f"역할 {role}의 토큰 {token!r}이 type_tokens에 없다")
        for name, t in tokens.items():
            if t.weight not in self.fonts.faces:
                raise ValueError(f"토큰 {name!r}의 굵기 {t.weight!r}에 대응하는 폰트 파일(fonts.faces)이 없다")
        return self


@dataclass(frozen=True)
class StyleDefaults:
    data: StyleDefaultsFile
    sha256: str  # 설정 파일 원본 바이트의 SHA-256
    path: Path

    @property
    def version(self) -> str:
        return self.data.defaults_version

    @property
    def base_dir(self) -> Path:
        return self.path.parent


@dataclass(frozen=True)
class RoleSpec:
    role: str
    type_token: str
    base_px: float
    face: str
    weight: int
    letter_spacing_pct: float


def load_style_defaults(path: str | Path | None = None) -> StyleDefaults:
    """설정 자료를 읽고 구조 · 대응을 검증한다. 경로를 주지 않으면 저장소 정본(STYLE_DEFAULTS_PATH)."""
    p = Path(path) if path else STYLE_DEFAULTS_PATH
    try:
        raw = p.read_bytes()
        data = StyleDefaultsFile.model_validate(json.loads(raw))
    except (OSError, ValueError, ValidationError) as e:
        raise StyleDefaultsError(f"스타일 기본값 자료를 읽을 수 없다({p}): {e.__class__.__name__}: {e}") from e
    return StyleDefaults(data=data, sha256=hashlib.sha256(raw).hexdigest(), path=p)


def role_spec(d: StyleDefaults, role: str) -> RoleSpec:
    if role not in ROLES:
        raise StyleInputInvalid(f"알 수 없는 역할 {role!r}")
    token = d.data.pipeline_mapping.roles[role]
    t = d.data.type_tokens.tokens[token]
    return RoleSpec(role=role, type_token=token, base_px=t.size_px, face=t.weight,
                    weight=d.data.fonts.faces[t.weight].weight, letter_spacing_pct=t.letter_spacing_pct)


def _check_width(source_width_px: Any) -> None:
    if not isinstance(source_width_px, int) or isinstance(source_width_px, bool) or source_width_px <= 0:
        raise StyleInputInvalid(f"원본 폭은 양의 정수 px여야 한다: {source_width_px!r}")


def default_font_px(d: StyleDefaults, role: str, source_width_px: int) -> float:
    """역할 기본 크기(원본 px) = 기준 크기 × 원본 폭 / 기준 폭. 반올림하지 않는다."""
    _check_width(source_width_px)
    return role_spec(d, role).base_px * source_width_px / d.data.pipeline_mapping.reference_width_px


# ---------------------------------------------------------------------------
# 폰트 파일 메타데이터 — 파일명이 아니라 sfnt 테이블(OS/2 · name)에서 읽는다
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FontMeta:
    outline: str  # "CFF" | "TrueType"
    weight_class: int  # OS/2 usWeightClass
    family: str | None  # name ID 16(없으면 1)
    subfamily: str | None  # name ID 17(없으면 2)
    postscript_name: str | None  # name ID 6
    version: str | None  # name ID 5


def read_font_meta(path: str | Path) -> FontMeta:
    b = Path(path).read_bytes()
    if len(b) < 12:
        raise FontAssetError(f"sfnt 파일이 아니다: {path}")
    tag = b[:4]
    if tag == b"OTTO":
        outline = "CFF"
    elif tag in (b"\x00\x01\x00\x00", b"true"):
        outline = "TrueType"
    else:
        raise FontAssetError(f"OTF/TTF가 아니다(시그니처 {tag!r}): {path}")
    (num,) = struct.unpack(">H", b[4:6])
    tables: dict[str, tuple[int, int]] = {}
    for i in range(num):
        t, _cs, off, ln = struct.unpack(">4sIII", b[12 + 16 * i: 28 + 16 * i])
        tables[t.decode("latin-1")] = (off, ln)
    if "OS/2" not in tables or "name" not in tables:
        raise FontAssetError(f"OS/2 · name 테이블이 없다: {path}")
    o, _ = tables["OS/2"]
    (weight_class,) = struct.unpack(">H", b[o + 4: o + 6])
    o, _ = tables["name"]
    _fmt, count, str_off = struct.unpack(">HHH", b[o: o + 6])
    names: dict[int, str] = {}
    for i in range(count):
        pid, eid, lid, nid, ln, off = struct.unpack(">6H", b[o + 6 + 12 * i: o + 18 + 12 * i])
        if pid == 3 and eid in (0, 1) and lid == 0x409:  # Windows · 영어(미국)
            s = o + str_off + off
            names[nid] = b[s: s + ln].decode("utf-16-be")
    return FontMeta(outline=outline, weight_class=weight_class, family=names.get(16) or names.get(1),
                    subfamily=names.get(17) or names.get(2), postscript_name=names.get(6), version=names.get(5))


def font_path(d: StyleDefaults, face: str) -> Path:
    return d.base_dir / d.data.fonts.faces[face].file


def verify_fonts(d: StyleDefaults) -> dict[str, FontMeta]:
    """배치된 폰트 · 라이선스 파일을 설정 자료와 대조한다. 하나라도 다르면 FontAssetError(시스템 폰트로 대체하지 않는다)."""
    problems: list[str] = []
    metas: dict[str, FontMeta] = {}
    fam = d.data.pipeline_mapping.font_family
    pkg = d.data.fonts.package
    lic = d.base_dir / pkg.license_file
    if not lic.is_file():
        problems.append(f"라이선스 파일 없음: {lic}")
    elif sha256_file(lic) != pkg.license_sha256:
        problems.append(f"라이선스 SHA-256 불일치: {lic}")
    for face, f in d.data.fonts.faces.items():
        p = d.base_dir / f.file
        if not p.is_file():
            problems.append(f"{face}: 폰트 파일 없음 {p} — `python -m pipeline.data.fonts.tools.fetch_fonts`로 배치")
            continue
        if p.stat().st_size != f.bytes or sha256_file(p) != f.sha256:
            problems.append(f"{face}: 크기 · SHA-256 불일치 {p}")
            continue
        m = read_font_meta(p)
        metas[face] = m
        if m.weight_class != f.weight:
            problems.append(f"{face}: usWeightClass {m.weight_class} ≠ 설정 {f.weight}")
        if m.family != fam or m.subfamily != face:
            problems.append(f"{face}: 이름 메타데이터 {m.family!r}/{m.subfamily!r} ≠ {fam!r}/{face!r}")
        if m.postscript_name != f.postscript_name or m.version != f.version:
            problems.append(f"{face}: PostScript 이름 · 버전 {m.postscript_name!r}/{m.version!r} ≠ 설정")
    if problems:
        raise FontAssetError("; ".join(problems))
    return metas


def render_inputs(d: StyleDefaults) -> dict[str, Any]:
    """렌더 입력 지문에 넣을 스타일 자료 식별. BE는 여기에 렌더러 · 라이브러리 버전과 배경 · 블록 입력을 더한다."""
    return {"style_defaults": {"version": d.version, "sha256": d.sha256},
            "fonts": {face: f.sha256 for face, f in sorted(d.data.fonts.faces.items())},
            "rule": RULE_VERSION}


def render_inputs_sha256(d: StyleDefaults) -> str:
    return sha256_canonical(render_inputs(d))


# ---------------------------------------------------------------------------
# 블록 적용값 계산
# ---------------------------------------------------------------------------
STYLE_KEYS = ("font_color", "bg_color", "est_font_px", "align")


@dataclass(frozen=True)
class BlockInput:
    id: int | str
    role: str
    is_excluded: bool | None  # false만 조판 대상(라벨 · 로고 · 규제 제외 등은 true)
    source_ko: str | None
    text: str | None  # 조판할 번역문(실패 칸 · 미입력은 None 또는 빈 문자열)
    style: dict[str, Any] | None  # ⑦ 측정 대표값 4키(측정 불가 NULL) 또는 객체 NULL
    style_stage: Literal["done", "failed"]  # 섹션의 ⑦ 단계 결과
    align_basis: str | None = None  # ⑦ 정렬 근거(그대로 전달. 역할 기본값과 구분)


@dataclass(frozen=True)
class ResolvedBlock:
    id: int | str
    render: bool
    skip_reason: str | None  # excluded | empty_source | no_translation
    style_stage: str
    measured: dict[str, Any] | None  # 입력 style 그대로(덮지 않음)
    measured_align_basis: str | None
    applied: dict[str, Any] | None
    source: dict[str, str] | None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "render": self.render, "skip_reason": self.skip_reason, "style_stage": self.style_stage,
                "measured": self.measured, "measured_align_basis": self.measured_align_basis,
                "applied": self.applied, "source": self.source}


def _check_measured(style: dict[str, Any], block_id: Any) -> None:
    if set(style) != set(STYLE_KEYS):
        raise StyleInputInvalid(f"블록 {block_id}: style 키는 {STYLE_KEYS} 4개여야 한다: {sorted(style)}")
    for k in ("font_color", "bg_color"):
        v = style[k]
        if v is not None and not (isinstance(v, str) and re.match(HEX_COLOR, v)):
            raise StyleInputInvalid(f"블록 {block_id}: {k} 형식 {v!r}(대문자 #RRGGBB 또는 NULL)")
    v = style["est_font_px"]
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0):
        raise StyleInputInvalid(f"블록 {block_id}: est_font_px {v!r}(유한한 양수 또는 NULL)")
    if style["align"] is not None and style["align"] not in ALIGNS:
        raise StyleInputInvalid(f"블록 {block_id}: align {style['align']!r}")


def resolve_block(b: BlockInput, source_width_px: int, d: StyleDefaults) -> ResolvedBlock:
    """블록 하나의 조판 적용값과 출처. 측정값은 measured에 그대로 두고 applied에 섞어 덮지 않는다."""
    if b.style_stage not in ("done", "failed"):
        raise StyleInputInvalid(f"블록 {b.id}: style_stage {b.style_stage!r}")
    measured = dict(b.style) if b.style is not None else None

    def skip(reason: str) -> ResolvedBlock:
        return ResolvedBlock(id=b.id, render=False, skip_reason=reason, style_stage=b.style_stage, measured=measured,
                             measured_align_basis=b.align_basis, applied=None, source=None)

    if b.is_excluded is not False:
        return skip("excluded")
    if not (b.source_ko or "").strip():
        return skip("empty_source")
    if not (b.text or "").strip():
        return skip("no_translation")

    if b.style_stage == "failed":
        if b.style is not None:
            raise StyleInputInvalid(f"블록 {b.id}: ⑦ 실패인데 style이 있다(이전 결과 혼입)")
        style: dict[str, Any] = {}
    else:
        if b.style is None:
            raise StyleInputInvalid(f"블록 {b.id}: ⑦ 완료 · 조판 대상인데 style 객체가 없다")
        _check_measured(b.style, b.id)
        style = b.style

    _check_width(source_width_px)
    spec = role_spec(d, b.role)
    m = d.data.pipeline_mapping
    source: dict[str, str] = {}

    def pick(key: str, measured_key: str, default: Any) -> Any:
        v = style.get(measured_key)
        source[key] = "measured" if v is not None else "role_default"
        return v if v is not None else default()

    # 측정 크기는 원본 px 그대로 — 기준 폭 배율 · em_ratio를 다시 곱하지 않는다
    size = pick("font_size_px", "est_font_px", lambda: default_font_px(d, b.role, source_width_px))
    color = pick("font_color", "font_color", lambda: m.font_color)
    align = pick("align", "align", lambda: m.align)
    face = d.data.fonts.faces[spec.face]
    ls_em = spec.letter_spacing_pct / 100
    source.update({"font_family": "role_config", "font_face": "role_config", "letter_spacing": "role_config",
                   "line_height": "role_config"})
    applied = {
        "type_token": spec.type_token,
        "font_family": m.font_family,
        "font_face": spec.face,
        "font_weight": spec.weight,
        "font_sha256": face.sha256,
        "font_size_px": size,
        "font_color": color,
        "align": align,
        "letter_spacing_em": ls_em,
        "letter_spacing_px": ls_em * size,
        "line_height_px": m.line_height_ratio * size,
    }
    return ResolvedBlock(id=b.id, render=True, skip_reason=None, style_stage=b.style_stage, measured=measured,
                         measured_align_basis=b.align_basis, applied=applied, source=source)
