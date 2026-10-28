# -*- coding: utf-8 -*-
"""基于 Sentence-Transformers 和 FAISS 的实体语义召回。"""

import hashlib
import json
import threading
from pathlib import Path
from typing import Optional
from typing import Protocol, Sequence

import faiss
import numpy as np

from graph_qa.entity_linker import EntityIndexEntry
from graph_qa.models import LinkedEntity


class SemanticIndexError(RuntimeError):
    """向量模型或索引不可用。"""


class TextEncoder(Protocol):
    def encode(
        self,
        texts: Sequence[str],
        *,
        batch_size: int,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> np.ndarray:
        ...


class SentenceTransformerEncoder:
    def __init__(self, model_name: str):
        self.model_name = model_name
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(model_name)
        except Exception as exc:
            raise SemanticIndexError("Sentence-Transformers 模型加载失败") from exc
        self._lock = threading.Lock()

    def encode(
        self,
        texts: Sequence[str],
        *,
        batch_size: int,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> np.ndarray:
        try:
            with self._lock:
                return np.asarray(
                    self._model.encode(
                        list(texts),
                        batch_size=batch_size,
                        normalize_embeddings=normalize_embeddings,
                        convert_to_numpy=convert_to_numpy,
                        show_progress_bar=show_progress_bar,
                    )
                )
        except Exception as exc:
            raise SemanticIndexError("实体向量编码失败") from exc


class SemanticEntityIndex:
    def __init__(
        self,
        entries: Sequence[EntityIndexEntry],
        encoder: TextEncoder,
        *,
        top_k: int = 3,
        threshold: float = 0.65,
        batch_size: int = 64,
        index: Optional[faiss.Index] = None,
        cache_hit: bool = False,
    ):
        if top_k < 1:
            raise ValueError("top_k 必须大于等于 1")
        if not 0 <= threshold <= 1:
            raise ValueError("threshold 必须在 0 到 1 之间")
        if batch_size < 1:
            raise ValueError("batch_size 必须大于等于 1")
        if not entries:
            raise ValueError("语义索引至少需要一个实体")

        self.entries = tuple(entries)
        self.encoder = encoder
        self.top_k = top_k
        self.threshold = threshold
        self.batch_size = batch_size
        self.cache_hit = cache_hit
        if index is not None:
            self._index = index
        else:
            texts = [self._entity_text(entry) for entry in self.entries]
            vectors = np.asarray(
                encoder.encode(
                    texts,
                    batch_size=batch_size,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                ),
                dtype=np.float32,
            )
            if vectors.ndim != 2 or len(vectors) != len(self.entries):
                raise SemanticIndexError("实体向量输出维度不正确")
            faiss.normalize_L2(vectors)
            self._index = faiss.IndexFlatIP(vectors.shape[1])
            self._index.add(vectors)

    @classmethod
    def load_or_build(
        cls,
        entries: Sequence[EntityIndexEntry],
        encoder: TextEncoder,
        *,
        cache_dir: Optional[Path] = None,
        cache_identity: str = "",
        top_k: int = 3,
        threshold: float = 0.5,
        batch_size: int = 64,
    ) -> "SemanticEntityIndex":
        if cache_dir is None:
            return cls(
                entries,
                encoder,
                top_k=top_k,
                threshold=threshold,
                batch_size=batch_size,
            )

        payload = json.dumps(
            [
                {
                    "label": entry.label,
                    "name": entry.name,
                    "aliases": list(entry.aliases),
                }
                for entry in entries
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(
            f"{cache_identity}\0{payload}".encode("utf-8")
        ).hexdigest()[:20]
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"entities-{fingerprint}.faiss"
        if cache_path.exists():
            try:
                cached_index = faiss.read_index(str(cache_path))
                return cls(
                    entries,
                    encoder,
                    top_k=top_k,
                    threshold=threshold,
                    batch_size=batch_size,
                    index=cached_index,
                    cache_hit=True,
                )
            except Exception:
                cache_path.unlink(missing_ok=True)

        result = cls(
            entries,
            encoder,
            top_k=top_k,
            threshold=threshold,
            batch_size=batch_size,
        )
        temporary_path = cache_path.with_suffix(".tmp")
        try:
            faiss.write_index(result._index, str(temporary_path))
            temporary_path.replace(cache_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
        return result

    @staticmethod
    def _entity_text(entry: EntityIndexEntry) -> str:
        aliases = "，".join(entry.aliases)
        return f"实体类型：{entry.label}；名称：{entry.name}；别名：{aliases}"

    def match(self, text: str) -> list[LinkedEntity]:
        if not text.strip() or not len(self.entries):
            return []
        vector = np.asarray(
            self.encoder.encode(
                [text],
                batch_size=1,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            ),
            dtype=np.float32,
        )
        if vector.ndim != 2 or vector.shape[0] != 1:
            raise SemanticIndexError("查询向量输出维度不正确")
        faiss.normalize_L2(vector)
        scores, indices = self._index.search(
            vector,
            min(self.top_k, len(self.entries)),
        )
        results = []
        for score, index in zip(scores[0], indices[0]):
            if index < 0 or float(score) < self.threshold:
                continue
            entry = self.entries[int(index)]
            results.append(
                LinkedEntity(
                    label=entry.label,
                    name=entry.name,
                    matched_text=text,
                    match_type="semantic",
                    score=round(float(score), 4),
                )
            )
        return results
