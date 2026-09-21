"""파라미터 정본(config/default.toml) 로더와 실행 인자 override.

- load_config()           기본 파일을 읽는다.
- load_config(path=...)   다른 파일로 통째로 바꾼다(실험용 사본).
- overrides=["merge.line_gap=0.8", "ocr.preprocess='gray'"]  값만 덮어쓴다. 값은 TOML 리터럴로 해석하고,
  해석에 실패하면 문자열로 둔다. 존재하지 않는 키는 오류다(오타 방지).
- get(cfg, "merge.line_gap")  점 표기로 읽는다.

문서 규칙(docs/ai/README.md 4.1): 실험 중 값 변경은 override로만 하고 파일과 pipeline.md 3절은 "확정" 때만 함께 고친다.
"""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any, Iterable

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "default.toml"


class ConfigKeyError(KeyError):
    pass


def load_config(path: str | Path | None = None, overrides: Iterable[str] = ()) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p, "rb") as f:
        cfg = tomllib.load(f)
    for item in overrides:
        apply_override(cfg, item)
    return cfg


def parse_override(item: str) -> tuple[str, Any]:
    if "=" not in item:
        raise ValueError(f"override 형식은 '표.키=값' — 받은 값: {item!r}")
    key, raw = item.split("=", 1)
    key = key.strip()
    raw = raw.strip()
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw  # 따옴표 없는 문자열
    return key, value


def apply_override(cfg: dict[str, Any], item: str) -> None:
    key, value = parse_override(item)
    parts = key.split(".")
    node = cfg
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            raise ConfigKeyError(f"config에 없는 표: {'.'.join(parts[:-1])!r}")
        node = node[part]
    if parts[-1] not in node:
        raise ConfigKeyError(f"config에 없는 키: {key!r} (미정 키는 코드에서 정하지 않는다)")
    node[parts[-1]] = value


def get(cfg: dict[str, Any], dotted: str, default: Any = ...) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            if default is ...:
                raise ConfigKeyError(f"config에 없는 키: {dotted!r}")
            return default
        node = node[part]
    return node


def flatten(cfg: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    """실행 기록(run.json)에 남길 복사본."""
    return copy.deepcopy(cfg)
