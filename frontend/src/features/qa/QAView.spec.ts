import { mount, flushPromises } from "@vue/test-utils";
import ElementPlus from "element-plus";
import { createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import QAView from "@/features/qa/QAView.vue";

const { streamQuestion, clearAnonymousSession } = vi.hoisted(() => ({
  streamQuestion: vi.fn(),
  clearAnonymousSession: vi.fn(),
}));

vi.mock("@/services/qaApi", () => ({
  ApiRequestError: class ApiRequestError extends Error {
    code = "REQUEST_FAILED";
    status = 500;
  },
  streamQuestion,
  clearAnonymousSession,
}));

describe("QAView", () => {
  beforeEach(() => {
    streamQuestion.mockReset();
    clearAnonymousSession.mockReset();
    Element.prototype.scrollIntoView = vi.fn();
  });

  it("starts from a recommendation and renders the evidence-backed answer", async () => {
    streamQuestion.mockImplementation(
      async (
        _question: string,
        onEvent: (event: {
          event: string;
          data: Record<string, unknown>;
        }) => Promise<void>,
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
          event: "done",
          data: {
            response: {
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
            },
          },
        });
      },
    );

    const wrapper = mount(QAView, {
      global: {
        plugins: [createPinia(), ElementPlus],
      },
    });

    const suggestions = wrapper.findAll(
      '[data-testid="suggested-question"]',
    );
    expect(suggestions.length).toBeGreaterThan(0);
    await suggestions[0]!.trigger("click");
    await flushPromises();
    await flushPromises();

    expect(wrapper.get('[data-testid="answer-body"]').text()).toContain(
      "麻黄具有发汗功效",
    );
    expect(wrapper.get('[data-testid="evidence-panel"]').text()).toContain(
      "《古籍》",
    );
    expect(wrapper.get('[data-testid="audit-card"]').text()).toContain(
      "1/1",
    );
    expect(wrapper.get('[data-testid="stage-list"]').text()).toContain(
      "匹配图谱实体",
    );
  });
});
