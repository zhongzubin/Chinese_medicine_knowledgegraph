# -*- coding: utf-8 -*-
"""FAISS 实体语义召回测试，不下载真实向量模型。"""

import numpy as np

from graph_qa.entity_linker import (
    EntityIndex,
    EntityIndexEntry,
    EntityRecord,
)
from graph_qa.models import EntityCandidate, LinkedEntity
from graph_qa.semantic_index import SemanticEntityIndex


class FakeEncoder:
    def encode(
        self,
        texts,
        *,
        batch_size,
        normalize_embeddings,
        convert_to_numpy,
        show_progress_bar,
    ):
        del batch_size, normalize_embeddings, convert_to_numpy, show_progress_bar
        vectors = []
        for text in texts:
            if "麻黄" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "桂枝" in text:
                vectors.append([0.0, 1.0, 0.0])
            elif "发汗" in text:
                vectors.append([0.0, 0.0, 1.0])
            else:
                vectors.append([0.0, 0.0, 0.0])
        return np.asarray(vectors, dtype=np.float32)


class SemanticEncoder(FakeEncoder):
    def encode(self, texts, **kwargs):
        vectors = []
        for text in texts:
            if "近似药材" in text:
                vectors.append([0.8, 0.7, 0.0])
            else:
                vectors.extend(super().encode([text], **kwargs))
        return np.asarray(vectors, dtype=np.float32)


def make_semantic_index(threshold=0.65):
    entries = [
        EntityIndexEntry("Herb", "麻黄", ("龙沙",)),
        EntityIndexEntry("Herb", "桂枝", ()),
        EntityIndexEntry("Effect", "发汗", ()),
    ]
    return SemanticEntityIndex(
        entries,
        SemanticEncoder(),
        top_k=3,
        threshold=threshold,
    )


def test_semantic_index_returns_top_three_above_threshold():
    index = make_semantic_index()

    results = index.match("近似药材")

    assert [result.name for result in results[:2]] == ["麻黄", "桂枝"]
    assert all(result.match_type == "semantic" for result in results)
    assert all(result.score >= 0.65 for result in results)


def test_semantic_index_filters_low_similarity():
    index = make_semantic_index(threshold=0.9)

    assert index.match("近似药材") == []


class FakeSemanticMatcher:
    def match(self, text):
        return [
            LinkedEntity(
                label="Herb",
                name="麻黄",
                matched_text=text,
                match_type="semantic",
                score=0.88,
            )
        ]


def test_entity_index_uses_semantic_fallback_when_exact_match_misses():
    index = EntityIndex.from_records(
        [EntityRecord("Herb", "麻黄", "龙沙")],
        semantic_index=FakeSemanticMatcher(),
    )

    results = index.match(
        [EntityCandidate(text="近似药材")],
        "近似药材有什么用？",
    )

    assert len(results) == 1
    assert results[0].name == "麻黄"
    assert results[0].match_type == "semantic"
