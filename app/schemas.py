"""공통 스키마 · enum 정의 (규격서 09_CHECK_enum 기준).

모든 응답은 mock(예시) 데이터다. 실제 DB·비즈니스 로직은 없다.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enum (규격서 09_CHECK_enum)
# ---------------------------------------------------------------------------
class JobStatus(str, Enum):
    draft = "draft"
    processing = "processing"
    review = "review"
    done = "done"
    failed = "failed"
    archived = "archived"


class CurrentStep(str, Enum):
    N1 = "N1"
    N2 = "N2"
    N3 = "N3"
    N4 = "N4"
    N5 = "N5"
    N6 = "N6"


class UserFacingStatus(str, Enum):
    draft = "draft"
    analyzing = "analyzing"
    section_review = "section_review"
    translating = "translating"
    reviewing = "reviewing"
    done = "done"
    failed = "failed"
    archived = "archived"


class SpecType(str, Enum):
    original = "original"
    site = "site"
    custom = "custom"


class VerdictStatus(str, Enum):
    regulated = "regulated"
    conditional = "conditional"
    irrelevant = "irrelevant"
    needs_fix = "needs_fix"
    policy = "policy"


class SectionBucket(str, Enum):
    include = "include"
    exclude = "exclude"


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class TaskType(str, Enum):
    ocr = "ocr"
    section = "section"
    inpaint = "inpaint"
    translate = "translate"
    verify = "verify"
    render = "render"


class BlockRole(str, Enum):
    title = "title"
    body = "body"
    caption = "caption"
    price = "price"
    caution = "caution"
    product_label = "product_label"


class BlockStatus(str, Enum):
    machine = "machine"
    edited = "edited"


class LogoFormat(str, Enum):
    svg = "svg"
    png = "png"
    jpg = "jpg"


class ArtifactType(str, Enum):
    zip = "zip"
    images = "images"
    csv = "csv"
    html = "html"
    manifest = "manifest"
    psd = "psd"
    baked = "baked"


# ---------------------------------------------------------------------------
# 공통 응답 모델
# ---------------------------------------------------------------------------
class ErrorBody(BaseModel):
    code: str = Field(examples=["VALIDATION_ERROR"])
    message: str = Field(examples=["요청 값이 올바르지 않습니다."])
    retryable: bool = False
    details: Optional[dict[str, Any]] = None
    traceId: Optional[str] = Field(default=None, examples=["trace-abc-123"])


class ErrorResponse(BaseModel):
    """규격서 §14-1 에러 래퍼: { error: { code, message, retryable, details?, traceId } }"""
    error: ErrorBody


class Bbox(BaseModel):
    x: int = 0
    y: int = 0
    w: int = 100
    h: int = 40
