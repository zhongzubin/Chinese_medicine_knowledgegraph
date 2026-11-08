# -*- coding: utf-8 -*-
"""模型适配器测试，不访问真实模型。"""

from graph_qa.llm import LangChainQAReasoner
from graph_qa.models import AttributeFact, Evidence, LinkedEntity


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeStreamingLLM:
    def __init__(self, chunks):
        self.chunks = chunks
        self.prompts = []

    def stream(self, messages):
        self.prompts.append(messages)
        for chunk in self.chunks:
            yield FakeMessage(chunk)


class FakeInvokeLLM:
    def __init__(self, content):
        self.content = content
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages)
        return FakeMessage(self.content)


def test_stream_direct_answer_emits_model_chunks():
    llm = FakeStreamingLLM(["量子", "计算"])
    reasoner = LangChainQAReasoner(llm=llm)
    received = []

    answer = reasoner.stream_direct_answer(
        "什么是量子计算？",
        received.append,
    )

    assert answer == "量子计算"
    assert received == ["量子", "计算"]
    assert len(llm.prompts) == 1


def test_graph_answer_returns_structured_claims():
    llm = FakeInvokeLLM(
        """
        {
          "claims": [
            {
              "text": "麻黄具有发汗功效，性味为辛、微苦，温。",
              "support_ids": ["E1", "F1"]
            }
          ]
        }
        """
    )
    reasoner = LangChainQAReasoner(llm=llm)

    answer = reasoner.answer_from_graph(
        "麻黄有什么功效？",
        [
            LinkedEntity(
                label="Herb",
                name="麻黄",
                matched_text="麻黄",
                match_type="name",
                score=1.0,
            )
        ],
        [
            {
                "subject": "麻黄",
                "subject_label": "Herb",
                "relationship": "HAS_EFFECT",
                "object": "发汗",
                "object_label": "Effect",
                "source": None,
            }
        ],
        [
            Evidence(
                subject="麻黄",
                subject_label="Herb",
                relationship="HAS_EFFECT",
                object="发汗",
                object_label="Effect",
            )
        ],
        [
            AttributeFact(
                subject="麻黄",
                subject_label="Herb",
                attribute="property_flavor",
                attribute_label="性味",
                value="辛、微苦，温",
                source=None,
            )
        ],
    )

    assert answer.claims[0].support_ids == ["E1", "F1"]
    assert answer.claims[0].text == "麻黄具有发汗功效，性味为辛、微苦，温。"
    assert len(llm.prompts) == 1
