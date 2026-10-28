# -*- coding: utf-8 -*-
"""基于图谱名称和别名的可解释实体链接。"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import (
    DefaultDict,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from graph_qa.models import EntityCandidate, LinkedEntity

NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)
ALIAS_SPLIT_RE = re.compile(r"[，,、；;/\n]+")


def normalize_entity_text(value: str) -> str:
    """统一全半角、大小写和空白，同时保留中文及字母数字。"""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return NORMALIZE_RE.sub("", normalized)


def split_aliases(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in ALIAS_SPLIT_RE.split(value) if part.strip()]


@dataclass(frozen=True)
class EntityRecord:
    label: str
    name: str
    alias: Optional[str] = None


@dataclass(frozen=True)
class EntityIndexEntry:
    label: str
    name: str
    aliases: Tuple[str, ...]

    def terms(self) -> Iterable[Tuple[str, str]]:
        yield self.name, "name"
        for alias in self.aliases:
            yield alias, "alias"


class SemanticMatcher(Protocol):
    def match(self, text: str) -> List[LinkedEntity]:
        ...


class EntityIndex:
    def __init__(
        self,
        entries: Sequence[EntityIndexEntry],
        semantic_index: Optional[SemanticMatcher] = None,
    ):
        self.entries = tuple(entries)
        self.semantic_index = semantic_index
        self._entries_by_label: Dict[str, List[EntityIndexEntry]] = {}
        self._exact_terms: DefaultDict[str, List[Tuple[EntityIndexEntry, str]]] = DefaultDict(list)

        for entry in self.entries:
            self._entries_by_label.setdefault(entry.label, []).append(entry)
            for term, match_type in entry.terms():
                normalized_term = normalize_entity_text(term)
                if normalized_term:
                    self._exact_terms[normalized_term].append((entry, match_type))

    @classmethod
    def from_records(
        cls,
        records: Iterable[EntityRecord],
        semantic_index: Optional[SemanticMatcher] = None,
    ) -> "EntityIndex":
        entries = [
            EntityIndexEntry(
                label=record.label,
                name=record.name,
                aliases=tuple(split_aliases(record.alias)),
            )
            for record in records
            if record.name.strip()
        ]
        return cls(entries, semantic_index=semantic_index)

    def __len__(self) -> int:
        return len(self.entries)

    def _allowed_entries(self, suggested_label: Optional[str]) -> Iterable[EntityIndexEntry]:
        if suggested_label:
            return self._entries_by_label.get(suggested_label, [])
        return self.entries

    def _exact_matches(
        self,
        text: str,
    ) -> List[LinkedEntity]:
        normalized_text = normalize_entity_text(text)
        if not normalized_text:
            return []
        results = []
        for entry, exact_type in self._exact_terms.get(normalized_text, []):
            results.append(
                LinkedEntity(
                    label=entry.label,
                    name=entry.name,
                    matched_text=text,
                    match_type=exact_type,
                    score=1.0,
                )
            )
        return results

    def _direct_question_matches(
        self,
        normalized_question: str,
        suggested_label: Optional[str],
    ) -> List[LinkedEntity]:
        results: List[Tuple[LinkedEntity, str]] = []
        if not normalized_question:
            return []
        for entry in self._allowed_entries(suggested_label):
            for term, match_type in entry.terms():
                normalized_term = normalize_entity_text(term)
                if len(normalized_term) < 2:
                    continue
                if normalized_term in normalized_question:
                    results.append(
                        (
                            LinkedEntity(
                                label=entry.label,
                                name=entry.name,
                                matched_text=term,
                                match_type=match_type,
                                score=0.96 if match_type == "name" else 0.94,
                            ),
                            normalized_term,
                        )
                    )
        longest_matches = []
        for item, normalized_term in results:
            if any(
                len(other_term) > len(normalized_term)
                and normalized_term in other_term
                for _other_item, other_term in results
            ):
                continue
            longest_matches.append(item)
        return longest_matches

    def _fuzzy_matches(
        self,
        candidate: EntityCandidate,
        fuzzy_threshold: float,
    ) -> List[LinkedEntity]:
        normalized_candidate = normalize_entity_text(candidate.text)
        if len(normalized_candidate) < 2:
            return []

        results = []
        for entry in self._allowed_entries(candidate.suggested_label):
            best_score = 0.0
            for term, _match_type in entry.terms():
                normalized_term = normalize_entity_text(term)
                if len(normalized_term) < 2:
                    continue
                score = SequenceMatcher(None, normalized_candidate, normalized_term).ratio()
                if score > best_score:
                    best_score = score
            if best_score >= fuzzy_threshold:
                results.append(
                    LinkedEntity(
                        label=entry.label,
                        name=entry.name,
                        matched_text=candidate.text,
                        match_type="fuzzy",
                        score=round(best_score, 4),
                    )
                )
        return results

    def match(
        self,
        candidates: Sequence[EntityCandidate],
        question: str,
        limit_per_label: int = 10,
        fuzzy_threshold: float = 0.72,
    ) -> List[LinkedEntity]:
        """合并模型候选、问句直接命中和模糊匹配，并保持标签隔离。"""
        if limit_per_label < 1:
            raise ValueError("limit_per_label 必须大于等于 1")

        normalized_question = normalize_entity_text(question)
        results: List[LinkedEntity] = []

        for candidate in candidates:
            result_count_before_candidate = len(results)
            exact_matches = self._exact_matches(candidate.text)
            if candidate.suggested_label:
                exact_matches.sort(
                    key=lambda item: (
                        item.label != candidate.suggested_label,
                        -item.score,
                    )
                )
            results.extend(exact_matches)
            if len(results) == result_count_before_candidate:
                fuzzy_matches = self._fuzzy_matches(candidate, fuzzy_threshold)
                if fuzzy_matches:
                    results.extend(fuzzy_matches)
                elif self.semantic_index is not None:
                    results.extend(self.semantic_index.match(candidate.text))

        if not results:
            results.extend(self._direct_question_matches(normalized_question, None))
        if not results and self.semantic_index is not None:
            results.extend(self.semantic_index.match(question))

        best_by_key: Dict[Tuple[str, str], LinkedEntity] = {}
        for result in results:
            key = (result.label, result.name)
            existing = best_by_key.get(key)
            if existing is None or result.score > existing.score:
                best_by_key[key] = result

        per_label_counts: DefaultDict[str, int] = DefaultDict(int)
        ordered = []
        for result in sorted(
            best_by_key.values(),
            key=lambda item: (-item.score, item.label, item.name),
        ):
            if per_label_counts[result.label] >= limit_per_label:
                continue
            per_label_counts[result.label] += 1
            ordered.append(result)
        return ordered
