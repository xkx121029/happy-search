"""检索链路内部数据模型（不对外暴露）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawResult:
    title: str
    url: str
    snippet: str
    source: str
    raw_score: float = 0.5
    rank_in_source: int = 0
    published_at: str | None = None
    domain: str | None = None
    content_type: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_WORD_RE = re.compile(r"[a-z0-9]+")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """轻量中英文分词：英文词 + 中文 2-gram（无第三方分词依赖）。"""
    text = (text or "").lower()
    words = _WORD_RE.findall(text)
    han_tokens: list[str] = []
    for seg in _HAN_RE.findall(text):
        if len(seg) == 1:
            han_tokens.append(seg)
        else:
            han_tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return words + han_tokens
