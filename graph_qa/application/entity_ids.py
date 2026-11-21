# -*- coding: utf-8 -*-
"""基于图谱业务键生成稳定、不带中文的外部实体 ID。"""

import hashlib
from typing import Dict, Iterable, Optional, Protocol, Tuple

from graph_qa.application.contracts import EntityRef


class EntityKeySource(Protocol):
    label: str
    name: str


def entity_id_for(label: str, name: str) -> str:
    digest = hashlib.sha256(f"{label}\0{name}".encode("utf-8")).hexdigest()
    return f"eid_{digest[:32]}"


class EntityIdMapper:
    def __init__(self, entities: Iterable[EntityKeySource]):
        self._keys_by_id: Dict[str, Tuple[str, str]] = {}
        self._ids_by_key: Dict[Tuple[str, str], str] = {}
        for entity in entities:
            key = (entity.label, entity.name)
            entity_id = entity_id_for(*key)
            existing = self._keys_by_id.get(entity_id)
            if existing is not None and existing != key:
                raise ValueError("EntityId 哈希冲突")
            self._keys_by_id[entity_id] = key
            self._ids_by_key[key] = entity_id

    def __len__(self) -> int:
        return len(self._ids_by_key)

    def entity_id(self, label: str, name: str) -> str:
        return entity_id_for(label, name)

    def entity_ref(self, label: str, name: str) -> EntityRef:
        return EntityRef(
            entity_id=self.entity_id(label, name),
            label=label,
            name=name,
        )

    def resolve(self, entity_id: str) -> Optional[EntityRef]:
        key = self._keys_by_id.get(entity_id)
        if key is None:
            return None
        return self.entity_ref(*key)
