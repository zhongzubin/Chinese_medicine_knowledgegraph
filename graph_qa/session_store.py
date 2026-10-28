# -*- coding: utf-8 -*-
"""跨实例共享的轻量会话历史与会话锁。"""

import json
from typing import List, Optional, Protocol

import redis
from pydantic import ValidationError

from graph_qa.api_security import APISettings
from graph_qa.models import ConversationTurn

RELEASE_LOCK_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""


class SessionStoreError(RuntimeError):
    """会话历史上读写失败。"""


class ConversationStore(Protocol):
    def ping(self) -> bool:
        ...

    def load(
        self,
        client_id: str,
        session_id: str,
    ) -> Optional[List[ConversationTurn]]:
        ...

    def save(
        self,
        client_id: str,
        session_id: str,
        turns: List[ConversationTurn],
    ) -> None:
        ...

    def delete(self, client_id: str, session_id: str) -> None:
        ...

    def acquire_lock(
        self,
        client_id: str,
        session_id: str,
        lock_token: str,
    ) -> bool:
        ...

    def release_lock(
        self,
        client_id: str,
        session_id: str,
        lock_token: str,
    ) -> None:
        ...

    def close(self) -> None:
        ...


class RedisConversationStore:
    def __init__(self, settings: APISettings):
        self._settings = settings
        self._client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            health_check_interval=30,
            max_connections=8,
        )
        self._release_lock_script = self._client.register_script(
            RELEASE_LOCK_SCRIPT
        )

    def _session_key(self, client_id: str, session_id: str) -> str:
        return (
            f"{self._settings.redis_prefix}:session:"
            f"{client_id}:{session_id}"
        )

    def _lock_key(self, client_id: str, session_id: str) -> str:
        return (
            f"{self._settings.redis_prefix}:session-lock:"
            f"{client_id}:{session_id}"
        )

    def ping(self) -> bool:
        return bool(self._client.ping())

    def load(
        self,
        client_id: str,
        session_id: str,
    ) -> Optional[List[ConversationTurn]]:
        try:
            raw = self._client.get(self._session_key(client_id, session_id))
        except Exception as exc:
            raise SessionStoreError("读取会话失败") from exc
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, list):
                raise ValueError("会话内容必须是数组")
            turns = [ConversationTurn.model_validate(item) for item in payload]
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
            raise SessionStoreError("会话内容格式无效") from exc
        return turns[-self._settings.session_max_turns :]

    def save(
        self,
        client_id: str,
        session_id: str,
        turns: List[ConversationTurn],
    ) -> None:
        trimmed = turns[-self._settings.session_max_turns :]
        payload = json.dumps(
            [item.model_dump(mode="json") for item in trimmed],
            ensure_ascii=False,
        )
        try:
            self._client.set(
                self._session_key(client_id, session_id),
                payload,
                ex=self._settings.session_ttl_seconds,
            )
        except Exception as exc:
            raise SessionStoreError("保存会话失败") from exc

    def delete(self, client_id: str, session_id: str) -> None:
        try:
            self._client.delete(self._session_key(client_id, session_id))
        except Exception as exc:
            raise SessionStoreError("删除会话失败") from exc

    def acquire_lock(
        self,
        client_id: str,
        session_id: str,
        lock_token: str,
    ) -> bool:
        try:
            return bool(
                self._client.set(
                    self._lock_key(client_id, session_id),
                    lock_token,
                    nx=True,
                    ex=self._settings.session_lock_seconds,
                )
            )
        except Exception as exc:
            raise SessionStoreError("获取会话锁失败") from exc

    def release_lock(
        self,
        client_id: str,
        session_id: str,
        lock_token: str,
    ) -> None:
        try:
            self._release_lock_script(
                keys=[self._lock_key(client_id, session_id)],
                args=[lock_token],
            )
        except Exception as exc:
            raise SessionStoreError("释放会话锁失败") from exc

    def close(self) -> None:
        self._client.close()
