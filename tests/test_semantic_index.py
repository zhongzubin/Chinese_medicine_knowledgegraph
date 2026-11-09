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


class FailOnBuildEncoder:
    def encode(self, texts, **kwargs):
        del texts, kwargs
        raise AssertionError("cached index should not rebuild")


class LabelFilterEncoder:
    def encode(self, texts, **kwargs):
        del kwargs
        vectors = []
        for text in texts:
            if "清除体内热毒" in text:
                vectors.append([1.0, 0.0, 0.0, 0.0])
            elif "热毒内盛" in text:
                vectors.append([1.0, 0.0, 0.0, 0.0])
            elif "热毒肿痛" in text:
                vectors.append([0.99, 0.1, 0.0, 0.0])
            elif "热毒疮肿" in text:
                vectors.append([0.98, 0.15, 0.0, 0.0])
            elif "清热解毒" in text:
                vectors.append([0.6, 0.8, 0.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 0.0, 1.0])
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


def test_entity_index_prefers_high_confidence_fuzzy_over_noisy_semantic():
    semantic = FakeSemanticMatcher()
    index = EntityIndex.from_records(
        [
            EntityRecord("Herb", "麻黄", "龙沙"),
            EntityRecord("Herb", "天麻"),
        ],
        semantic_index=semantic,
    )

    results = index.match(
        [EntityCandidate(text="麻黄草")],
        "麻黄草有什么功效？",
    )

    assert results[0].name == "麻黄"
    assert results[0].match_type == "fuzzy"
    assert all(item.match_type != "semantic" for item in results)


def test_semantic_index_label_filter_finds_lower_ranked_target_label():
    index = SemanticEntityIndex(
        [
            EntityIndexEntry("Disease", "热毒内盛", ()),
            EntityIndexEntry("Disease", "热毒肿痛", ()),
            EntityIndexEntry("Disease", "热毒疮肿", ()),
            EntityIndexEntry("Effect", "清热解毒", ()),
        ],
        LabelFilterEncoder(),
        top_k=3,
        threshold=0.5,
    )

    default_results = index.match("清除体内热毒")
    filtered_results = index.match(
        "清除体内热毒",
        labels=("Effect",),
        candidate_limit=20,
    )

    assert {item.label for item in default_results} == {"Disease"}
    assert [(item.label, item.name) for item in filtered_results] == [
        ("Effect", "清热解毒")
    ]


def test_semantic_faiss_index_is_reused_from_disk_cache(tmp_path):
    entries = [
        EntityIndexEntry("Herb", "麻黄", ("龙沙",)),
        EntityIndexEntry("Herb", "桂枝", ()),
    ]
    first = SemanticEntityIndex.load_or_build(
        entries,
        SemanticEncoder(),
        cache_dir=tmp_path,
        cache_identity="local-bge",
        threshold=0.5,
    )
    second = SemanticEntityIndex.load_or_build(
        entries,
        FailOnBuildEncoder(),
        cache_dir=tmp_path,
        cache_identity="local-bge",
        threshold=0.5,
    )

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert len(list(tmp_path.glob("entities-*.faiss"))) == 1
