import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GraphQAResponse, QAStreamEvent } from "@/types/api";

const { streamQuestion, clearAnonymousSession } = vi.hoisted(() => ({
  streamQuestion: vi.fn(),
  clearAnonymousSession: vi.fn(),
}));

vi.mock("@/services/qaApi", () => ({
  ApiRequestError: class ApiRequestError extends Error {
    code: string;
    status: number;

    constructor(status: number, code: string, message: string) {
      super(message);
      this.status = status;
      this.code = code;
    }
  },
  streamQuestion,
  clearAnonymousSession,
}));

import { useQAStore } from "@/stores/qa";

function response(): GraphQAResponse {
  return {
    schema_version: "1.0",
    request_id: "run-1",
    question: "麻黄有什么功效？",
    answer: "麻黄具有发汗功效。",
    grounded: true,
    linked_entities: [],
    query: null,
    evidence: [
      {
        subject: "麻黄",
        subject_label: "Herb",
        relationship: "HAS_EFFECT",
        object: "发汗",
        object_label: "Effect",
        source: "《古籍》",
      },
    ],
    facts: [],
    aggregates: [],
    paths: [],
    verification: {
      status: "passed",
      agent: "evidence_audit",
      checked_items: 1,
      matched_items: 1,
      evidence_items: 1,
      fact_items: 0,
      aggregate_items: 0,
      path_items: 0,
      missing_sources: 0,
      fallback_used: false,
      message: "已核对全部图谱证据。",
    },
    warnings: [],
    error: null,
    session_id: null,
  };
}

describe("QA store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    streamQuestion.mockReset();
    clearAnonymousSession.mockReset();
  });

  it("assembles streamed stages, evidence, answer and audit", async () => {
    const finalResponse = response();
    streamQuestion.mockImplementation(
      async (
        _question: string,
        onEvent: (event: QAStreamEvent) => Promise<void>,
      ) => {
        await onEvent({
          event: "stage",
          data: {
            stage: "link_entities",
            label: "匹配图谱实体",
            agent: "entity",
          },
        });
        await onEvent({
          event: "answer_delta",
          data: { delta: "麻黄具有" },
        });
        await onEvent({
          event: "done",
          data: { response: finalResponse },
        });
      },
    );

    const store = useQAStore();
    await store.ask("  麻黄有什么功效？ ");

    expect(streamQuestion).toHaveBeenCalledOnce();
    expect(store.stages[0]!.label).toBe("匹配图谱实体");
    expect(store.answer).toBe("麻黄具有发汗功效。");
    expect(store.evidence[0]!.object).toBe("发汗");
    expect(store.verification?.status).toBe("passed");
    expect(store.isStreaming).toBe(false);
    expect(store.degraded).toBe(false);
  });

  it("turns request errors into stable UI state", async () => {
    streamQuestion.mockRejectedValue(new Error("offline"));
    const store = useQAStore();

    await store.ask("麻黄有什么功效？");

    expect(store.errorCode).toBe("NETWORK_ERROR");
    expect(store.errorMessage).toContain("无法连接");
    expect(store.isStreaming).toBe(false);
  });

  it("clears the anonymous session before starting over", async () => {
    clearAnonymousSession.mockResolvedValue(undefined);
    streamQuestion.mockImplementation(
      async (
        _question: string,
        onEvent: (event: QAStreamEvent) => Promise<void>,
      ) => {
        await onEvent({
          event: "done",
          data: { response: response() },
        });
      },
    );
    const store = useQAStore();
    await store.ask("麻黄有什么功效？");

    await store.startNewConversation();

    expect(clearAnonymousSession).toHaveBeenCalledOnce();
    expect(store.answer).toBe("");
    expect(store.activeQuestion).toBe("");
  });
});
