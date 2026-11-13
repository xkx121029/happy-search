"""本地索引 Provider：零依赖的内存索引 + JSON 文件持久化。

M1 用 token 重叠打分近似 BM25；M2 可无缝替换为 Meilisearch / Qdrant。
同时服务 local_index 与 knowledge_base 两个源。
"""
from __future__ import annotations

import json
import math
import os
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from ..config import settings
from ..core.models import RawResult, tokenize


class LocalIndex:
    def __init__(self, path: str | None = None, max_documents: int | None = None) -> None:
        self._path = Path(path or settings.local_docs_path)
        self.max_documents = max_documents or settings.local_max_documents
        self._docs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("documents"), list):
                self._docs = {str(d["id"]): d for d in raw["documents"] if "id" in d}
        except (OSError, ValueError, KeyError, TypeError):
            self._docs = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换，避免写入中途崩溃导致数据文件损坏
        tmp = self._path.with_name(self._path.name + f".tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps({"documents": list(self._docs.values())}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    @property
    def total(self) -> int:
        return len(self._docs)

    def add(self, documents: list[dict]) -> list[str]:
        if self._docs and len(self._docs) + len(documents) > self.max_documents:
            raise ValueError(
                f"本地索引文档数已达上限 {self.max_documents}；"
                f"可删除部分文档或调大 HAPPYSEARCH_LOCAL_MAX_DOCS"
            )
        ids: list[str] = []
        with self._lock:
            for doc in documents:
                doc_id = doc.get("id") or ("doc_" + uuid.uuid4().hex[:12])
                stored = dict(doc)
                stored["id"] = doc_id
                if stored.get("url"):
                    stored["domain"] = urlparse(stored["url"]).netloc or None
                self._docs[doc_id] = stored
                ids.append(doc_id)
            self._save()
        return ids

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        source: str = "local_index",
        tags: list[str] | None = None,
    ) -> list[RawResult]:
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        with self._lock:  # 快照，避免与并发写入产生不一致
            docs = list(self._docs.values())
        q_counts: dict[str, int] = {}
        for t in q_tokens:
            q_counts[t] = q_counts.get(t, 0) + 1

        scored: list[tuple[float, dict]] = []
        for doc in docs:
            if tags and not set(tags).intersection(doc.get("tags") or []):
                continue
            d_tokens = tokenize(f"{doc.get('title', '')} {doc.get('content', '')}")
            if not d_tokens:
                continue
            d_counts: dict[str, int] = {}
            for t in d_tokens:
                d_counts[t] = d_counts.get(t, 0) + 1
            dot = sum(c * d_counts.get(t, 0) for t, c in q_counts.items())
            if dot <= 0:
                continue
            norm = math.sqrt(sum(c * c for c in q_counts.values())) * math.sqrt(
                sum(c * c for c in d_counts.values())
            )
            score = min(dot / norm, 1.0) if norm else 0.0
            scored.append((score, doc))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        out: list[RawResult] = []
        for rank, (score, doc) in enumerate(scored[:limit], start=1):
            content = doc.get("content", "")
            out.append(
                RawResult(
                    title=doc.get("title", ""),
                    url=doc.get("url") or f"local://{doc['id']}",
                    snippet=content[:300].replace("\n", " "),
                    source=source,
                    raw_score=score,
                    rank_in_source=rank,
                    published_at=doc.get("published_at"),
                    domain=doc.get("domain"),
                    content_type="text/plain",
                    extra={"doc_id": doc["id"], "content": content},
                )
            )
        return out


index = LocalIndex()
