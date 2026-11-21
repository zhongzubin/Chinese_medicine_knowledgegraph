import { defineStore } from "pinia";

import {
  ApiRequestError,
  clearAnonymousSession,
  streamQuestion,
} from "@/services/qaApi";
import type {
  AggregateFact,
  AnswerVerification,
  AttributeFact,
  Evidence,
  GraphQAResponse,
  PathEvidence,
  QAStreamEvent,
  StagePayload,
} from "@/types/api";

let activeController: AbortController | null = null;
let requestGeneration = 0;

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

export const useQAStore = defineStore("qa", {
  state: () => ({
    question: "",
    activeQuestion: "",
    isStreaming: false,
    stages: [] as StagePayload[],
    answer: "",
    grounded: false,
    evidence: [] as Evidence[],
    facts: [] as AttributeFact[],
    aggregates: [] as AggregateFact[],
    paths: [] as PathEvidence[],
    verification: null as AnswerVerification | null,
    warnings: [] as string[],
    errorCode: "",
    errorMessage: "",
    response: null as GraphQAResponse | null,
    doneSeen: false,
  }),
  getters: {
    busy: (state) => state.isStreaming,
    hasResult: (state) =>
      Boolean(
        state.answer ||
          state.evidence.length ||
          state.facts.length ||
          state.paths.length ||
          state.errorMessage,
      ),
    degraded: (state) =>
      Boolean(
        state.errorMessage ||
          state.verification?.fallback_used ||
          state.warnings.some((message) => message.includes("降级")),
      ),
  },
  actions: {
    resetCurrentResult() {
      this.stages = [];
      this.answer = "";
      this.grounded = false;
      this.evidence = [];
      this.facts = [];
      this.aggregates = [];
      this.paths = [];
      this.verification = null;
      this.warnings = [];
      this.errorCode = "";
      this.errorMessage = "";
      this.response = null;
      this.doneSeen = false;
    },

    applyEvent(event: QAStreamEvent) {
      const { data } = event;
      if (event.event === "stage") {
        const stage = data as unknown as StagePayload;
        if (stage.stage && stage.label) {
          this.stages.push(stage);
        }
        return;
      }
      if (event.event === "evidence") {
        this.evidence = asArray<Evidence>(data.items);
        return;
      }
      if (event.event === "facts") {
        this.facts = asArray<AttributeFact>(data.items);
        return;
      }
      if (event.event === "aggregates") {
        this.aggregates = asArray<AggregateFact>(data.items);
        return;
      }
      if (event.event === "paths") {
        this.paths = asArray<PathEvidence>(data.items);
        return;
      }
      if (event.event === "audit") {
        this.verification = data as unknown as AnswerVerification;
        return;
      }
      if (event.event === "answer_delta") {
        this.answer += typeof data.delta === "string" ? data.delta : "";
        return;
      }
      if (event.event === "warning") {
        if (typeof data.message === "string" && data.message) {
          this.warnings.push(data.message);
        }
        return;
      }
      if (event.event === "error") {
        this.errorCode =
          typeof data.code === "string" ? data.code : "UNKNOWN_ERROR";
        this.errorMessage =
          typeof data.message === "string"
            ? data.message
            : "问答处理失败";
        return;
      }
      if (event.event === "done") {
        const response = data.response as GraphQAResponse | undefined;
        if (!response) {
          this.errorCode = "INVALID_RESPONSE";
          this.errorMessage = "服务端未返回完整回答";
          return;
        }
        this.doneSeen = true;
        this.response = response;
        this.answer = response.answer || this.answer;
        this.grounded = response.grounded;
        this.evidence = response.evidence || [];
        this.facts = response.facts || [];
        this.aggregates = response.aggregates || [];
        this.paths = response.paths || [];
        this.verification = response.verification;
        this.warnings = response.warnings || [];
        this.errorCode = response.error?.code || "";
        this.errorMessage = response.error?.message || "";
      }
    },

    async ask(question: string) {
      const normalized = question.trim();
      if (!normalized || this.isStreaming) {
        return;
      }
      activeController?.abort();
      activeController = new AbortController();
      const generation = ++requestGeneration;
      this.resetCurrentResult();
      this.question = normalized;
      this.activeQuestion = normalized;
      this.isStreaming = true;

      try {
        await streamQuestion(
          normalized,
          async (event) => {
            if (generation === requestGeneration) {
              this.applyEvent(event);
            }
          },
          activeController.signal,
        );
        if (generation === requestGeneration && !this.doneSeen) {
          this.errorCode = "STREAM_INCOMPLETE";
          this.errorMessage = "流式响应意外结束，请稍后重试";
        }
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          if (generation === requestGeneration) {
            this.warnings.push("本轮请求已取消");
          }
          return;
        }
        if (generation !== requestGeneration) {
          return;
        }
        if (error instanceof ApiRequestError) {
          this.errorCode = error.code;
          this.errorMessage = error.message;
        } else {
          this.errorCode = "NETWORK_ERROR";
          this.errorMessage = "无法连接问答服务，请检查网络后重试";
        }
      } finally {
        if (generation === requestGeneration) {
          this.isStreaming = false;
        }
      }
    },

    cancel() {
      activeController?.abort();
      activeController = null;
      this.isStreaming = false;
    },

    async startNewConversation() {
      activeController?.abort();
      activeController = null;
      requestGeneration += 1;
      this.isStreaming = false;
      let clearCode = "";
      let clearMessage = "";
      try {
        await clearAnonymousSession();
      } catch (error) {
        clearCode =
          error instanceof ApiRequestError
            ? error.code
            : "SESSION_CLEAR_FAILED";
        clearMessage =
          error instanceof Error
            ? error.message
            : "无法清空当前会话";
      }
      this.question = "";
      this.activeQuestion = "";
      this.resetCurrentResult();
      this.errorCode = clearCode;
      this.errorMessage = clearMessage;
    },
  },
});
