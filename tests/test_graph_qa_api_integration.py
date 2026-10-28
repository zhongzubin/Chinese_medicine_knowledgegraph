# -*- coding: utf-8 -*-
"""真实模型、Neo4j、Redis 与 SSE 的可选 API 集成测试。"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from graph_qa.api import create_app
from graph_qa.api_security import APISettings

pytestmark = pytest.mark.skipif(
    os.getenv("GRAPH_QA_RUN_API_INTEGRATION") != "1",
    reason="设置 GRAPH_QA_RUN_API_INTEGRATION=1 后才运行真实 API 集成测试",
)


def _events(text):
    result = []
    event_name = None
    data_lines = []
    for line in text.splitlines():
        if not line:
            if event_name and data_lines:
                result.append((event_name, json.loads("\n".join(data_lines))))
            event_name = None
            data_lines = []
        elif line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())
    return result


def test_same_session_is_shared_across_api_instances():
    settings = APISettings.from_environment()
    api_key = next(iter(settings.api_keys.values()))
    headers = {"X-API-Key": api_key}
    first_app = create_app(settings=settings)
    second_app = create_app(settings=settings)

    with TestClient(first_app) as first_client:
        first_response = first_client.post(
            "/api/v1/qa/stream",
            headers=headers,
            json={"question": "麻黄有什么功效？"},
        )

    assert first_response.status_code == 200
    first_events = _events(first_response.text)
    first_done = next(data for name, data in first_events if name == "done")
    first_payload = first_done["response"]
    session_id = first_payload["session_id"]
    assert session_id
    assert first_payload["grounded"] is True
    assert any(name == "answer_delta" for name, _data in first_events)

    with TestClient(second_app) as second_client:
        second_response = second_client.post(
            "/api/v1/qa/stream",
            headers=headers,
            json={
                "question": "它还能治疗哪些疾病？",
                "session_id": session_id,
            },
        )
        second_client.delete(
            f"/api/v1/sessions/{session_id}",
            headers=headers,
        )

    assert second_response.status_code == 200
    second_events = _events(second_response.text)
    second_done = next(data for name, data in second_events if name == "done")
    second_payload = second_done["response"]
    assert second_payload["session_id"] == session_id
    assert "麻黄" in second_payload["rewritten_question"]
