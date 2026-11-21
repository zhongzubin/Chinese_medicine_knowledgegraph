<script setup lang="ts">
import { nextTick, ref } from "vue";
import { storeToRefs } from "pinia";

import { useQAStore } from "@/stores/qa";
import AnswerPanel from "@/features/qa/components/AnswerPanel.vue";
import AuditCard from "@/features/qa/components/AuditCard.vue";
import EvidencePanel from "@/features/qa/components/EvidencePanel.vue";
import QuestionComposer from "@/features/qa/components/QuestionComposer.vue";
import StageTimeline from "@/features/qa/components/StageTimeline.vue";
import StatusNotice from "@/features/qa/components/StatusNotice.vue";

const store = useQAStore();
const {
  answer,
  aggregates,
  busy,
  degraded,
  errorCode,
  errorMessage,
  evidence,
  facts,
  grounded,
  hasResult,
  paths,
  stages,
  verification,
  warnings,
  activeQuestion,
} = storeToRefs(store);

const resultAnchor = ref<HTMLElement | null>(null);
const composerKey = ref(0);

const suggestions = [
  {
    title: "药材属性与来源",
    question: "麻黄有什么功效？图谱中是否有来源？",
    tag: "属性",
  },
  {
    title: "反向关系检索",
    question: "含麻黄的方剂有哪些？",
    tag: "关系",
  },
  {
    title: "受控多跳取证",
    question: "四君子汤所含药材有什么功效？",
    tag: "多跳",
  },
];

async function ask(question: string) {
  await store.ask(question);
  await nextTick();
  resultAnchor.value?.scrollIntoView({
    behavior: "smooth",
    block: "start",
  });
}

async function startNewConversation() {
  await store.startNewConversation();
  composerKey.value += 1;
}
</script>

<template>
  <div class="qa-layout">
    <aside class="qa-sidebar" aria-label="推荐问题与会话说明">
      <section class="sidebar-card surface">
        <span class="eyebrow">Evidence first</span>
        <h1>从问题走到<br />可核对的图谱证据</h1>
        <p>
          问题先经过实体链接和受控查询，再由证据审核约束最终回答。
          没有图谱依据时，系统会明确说明。
        </p>
        <div class="sidebar-card__signals">
          <span>只读查询</span>
          <span>来源披露</span>
          <span>逐项审核</span>
        </div>
      </section>

      <section class="suggestion-panel" aria-labelledby="suggestion-title">
        <div class="section-heading">
          <div>
            <span class="eyebrow">Scenarios</span>
            <h2 id="suggestion-title">推荐问题</h2>
          </div>
        </div>
        <button
          v-for="suggestion in suggestions"
          :key="suggestion.question"
          class="suggestion"
          data-testid="suggested-question"
          :disabled="busy"
          @click="ask(suggestion.question)"
        >
          <span class="suggestion__tag">{{ suggestion.tag }}</span>
          <strong>{{ suggestion.title }}</strong>
          <span>{{ suggestion.question }}</span>
        </button>
      </section>

      <section class="session-note">
        <strong>匿名演示会话</strong>
        <p>问题与回答仅保留最近 5 轮，用于上下文追问。</p>
        <el-button
          text
          data-testid="new-session"
          :disabled="busy"
          @click="startNewConversation"
        >
          新建会话
        </el-button>
      </section>
    </aside>

    <section class="qa-main">
      <header class="qa-hero surface">
        <div>
          <span class="eyebrow">Evidence Q&amp;A</span>
          <h2>证据问答</h2>
          <p>
            查看公开处理阶段、结构化事实、来源状态和回答审核结果。
          </p>
        </div>
        <div class="qa-hero__badge" aria-label="系统只读查询">
          <span>Neo4j</span>
          <strong>只读</strong>
          <small>Guard + EXPLAIN</small>
        </div>
      </header>

      <QuestionComposer
        :key="composerKey"
        :busy="busy"
        @submit="ask"
        @cancel="store.cancel"
      />

      <StatusNotice
        :error-code="errorCode"
        :error-message="errorMessage"
        :warnings="warnings"
        :degraded="degraded"
      />

      <div v-if="activeQuestion || busy" class="question-banner">
        <span>本轮问题</span>
        <strong data-testid="active-question">
          {{ activeQuestion || "正在准备问题…" }}
        </strong>
      </div>

      <div ref="resultAnchor" class="qa-grid">
        <StageTimeline :stages="stages" :busy="busy" />
        <AnswerPanel
          :answer="answer"
          :grounded="grounded"
          :busy="busy"
        />
      </div>

      <AuditCard
        v-if="verification"
        :verification="verification"
      />

      <EvidencePanel
        :evidence="evidence"
        :facts="facts"
        :aggregates="aggregates"
        :paths="paths"
      />

      <section
        v-if="!hasResult && !busy"
        class="empty-state surface"
        aria-labelledby="empty-title"
      >
        <div class="empty-state__mark" aria-hidden="true">证</div>
        <div>
          <span class="eyebrow">Start here</span>
          <h2 id="empty-title">从一个可验证的问题开始</h2>
          <p>
            可以询问药材功效、方剂组成、来源缺失或多跳关系。
            回答加载后会同时展示证据与审核状态。
          </p>
        </div>
      </section>
    </section>
  </div>
</template>

<style scoped>
.qa-layout {
  display: grid;
  grid-template-columns: 286px minmax(0, 1fr);
  gap: 24px;
  padding: 28px 0 40px;
}

.qa-sidebar {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.sidebar-card {
  padding: 22px;
  border-radius: 20px;
  background:
    linear-gradient(145deg, rgba(255, 253, 248, 0.98), rgba(238, 246, 242, 0.98));
}

.sidebar-card h1 {
  margin: 10px 0 14px;
  font-family: "STKaiti", "KaiTi", serif;
  font-size: clamp(27px, 2.6vw, 35px);
  line-height: 1.3;
  letter-spacing: 0.02em;
}

.sidebar-card p {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.8;
}

.sidebar-card__signals {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 18px;
}

.sidebar-card__signals span {
  padding: 5px 8px;
  border: 1px solid rgba(23, 107, 91, 0.17);
  border-radius: 999px;
  color: var(--jade-dark);
  font-size: 10px;
}

.section-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  margin-bottom: 10px;
}

.section-heading h2 {
  margin: 4px 0 0;
  font-size: 16px;
}

.suggestion-panel {
  display: grid;
  gap: 8px;
}

.suggestion {
  display: grid;
  gap: 5px;
  width: 100%;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: rgba(255, 253, 248, 0.82);
  color: var(--ink);
  text-align: left;
  cursor: pointer;
  transition:
    transform 160ms ease,
    border-color 160ms ease,
    box-shadow 160ms ease;
}

.suggestion:hover:not(:disabled) {
  transform: translateY(-2px);
  border-color: rgba(23, 107, 91, 0.45);
  box-shadow: 0 10px 28px rgba(52, 43, 29, 0.08);
}

.suggestion:disabled {
  cursor: wait;
  opacity: 0.6;
}

.suggestion__tag {
  width: max-content;
  padding: 2px 6px;
  border-radius: 999px;
  background: var(--paper-strong);
  color: var(--cinnabar);
  font-size: 9px;
}

.suggestion strong {
  font-size: 13px;
}

.suggestion > span:last-child {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.55;
}

.session-note {
  padding: 4px 4px 0;
}

.session-note strong {
  font-size: 13px;
}

.session-note p {
  margin: 5px 0 3px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.6;
}

.qa-main {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
}

.qa-hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 22px 24px;
  border-radius: 20px;
}

.qa-hero h2 {
  margin: 6px 0 5px;
  font-size: clamp(26px, 3vw, 38px);
  letter-spacing: -0.035em;
}

.qa-hero p {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
}

.qa-hero__badge {
  flex: 0 0 auto;
  display: grid;
  justify-items: center;
  gap: 2px;
  padding: 13px 18px;
  border: 1px solid rgba(23, 107, 91, 0.2);
  border-radius: 16px;
  background: var(--jade-soft);
  color: var(--jade-dark);
}

.qa-hero__badge span,
.qa-hero__badge small {
  font-size: 9px;
  letter-spacing: 0.08em;
}

.qa-hero__badge strong {
  font-size: 19px;
}

.question-banner {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 2px 4px;
}

.question-banner span {
  color: var(--cinnabar);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.12em;
}

.question-banner strong {
  font-size: 16px;
  line-height: 1.5;
}

.qa-grid {
  display: grid;
  grid-template-columns: minmax(270px, 0.8fr) minmax(0, 1.5fr);
  gap: 18px;
  scroll-margin-top: 96px;
}

.empty-state {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 24px;
  border-radius: 18px;
}

.empty-state__mark {
  flex: 0 0 auto;
  width: 58px;
  height: 58px;
  display: grid;
  place-items: center;
  border-radius: 18px;
  background: var(--paper-strong);
  color: var(--gold);
  font-family: "STKaiti", "KaiTi", serif;
  font-size: 30px;
}

.empty-state h2 {
  margin: 5px 0;
  font-size: 18px;
}

.empty-state p {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.7;
}

@media (max-width: 1080px) {
  .qa-layout {
    grid-template-columns: 232px minmax(0, 1fr);
  }

  .qa-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 760px) {
  .qa-layout {
    grid-template-columns: 1fr;
    padding-top: 16px;
  }

  .qa-sidebar {
    display: grid;
    grid-template-columns: 1fr;
  }

  .sidebar-card {
    padding: 18px;
  }

  .sidebar-card h1 {
    font-size: 27px;
  }

  .suggestion-panel {
    grid-template-columns: 1fr;
  }

  .session-note {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .session-note p {
    display: none;
  }

  .qa-hero {
    align-items: flex-start;
    padding: 18px;
  }

  .qa-hero__badge {
    display: none;
  }

  .question-banner {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }

  .empty-state {
    align-items: flex-start;
    padding: 18px;
  }
}
</style>
