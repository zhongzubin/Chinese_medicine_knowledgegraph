# -*- coding: utf-8 -*-
"""基于 Sentence-Transformers 和 FAISS 的实体语义召回。"""

import threading
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
