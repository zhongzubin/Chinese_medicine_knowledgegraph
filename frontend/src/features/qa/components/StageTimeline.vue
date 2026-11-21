<script setup lang="ts">
import type { StagePayload } from "@/types/api";

defineProps<{
  stages: StagePayload[];
  busy: boolean;
}>();

const agentLabels: Record<string, string> = {
  dialogue: "对话理解",
  intent: "意图识别",
  entity: "实体链接",
  retrieval: "图谱检索",
  answer: "答案组织",
  evidence_audit: "证据审核",
};
</script>

<template>
  <section class="timeline surface" aria-labelledby="timeline-title">
    <div class="timeline__heading">
      <div>
        <span class="eyebrow">Trace</span>
        <h2 id="timeline-title">公开处理过程</h2>
      </div>
      <span class="timeline__status" :class="{ 'is-running': busy }">
        {{ busy ? "处理中" : stages.length ? `${stages.length} 个阶段` : "等待提问" }}
      </span>
    </div>

    <ol v-if="stages.length" class="timeline__list" data-testid="stage-list">
      <li v-for="(stage, index) in stages" :key="`${stage.stage}-${index}`">
        <span class="timeline__dot" aria-hidden="true"></span>
        <div>
          <div class="timeline__title">
            <strong>{{ stage.label || stage.stage }}</strong>
            <span v-if="stage.agent">
              {{ agentLabels[stage.agent] || stage.agent }}
            </span>
          </div>
          <p v-if="stage.plan_label || stage.decision">
            {{ stage.plan_label || stage.decision }}
            <span v-if="stage.reason_code">· {{ stage.reason_code }}</span>
          </p>
        </div>
      </li>
    </ol>
    <p v-else class="timeline__empty">
      提问后将显示意图识别、实体链接、图谱查询和证据审核等公开阶段。
    </p>
  </section>
</template>

<style scoped>
.timeline {
  padding: 20px;
  border-radius: 18px;
}

.timeline__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}

h2 {
  margin: 5px 0 0;
  font-size: 18px;
}

.timeline__status {
  padding: 5px 9px;
  border-radius: 999px;
  background: var(--paper-strong);
  color: var(--muted);
  font-size: 11px;
}

.timeline__status.is-running {
  background: var(--jade-soft);
  color: var(--jade-dark);
}

.timeline__list {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.timeline__list li {
  position: relative;
  display: grid;
  grid-template-columns: 18px 1fr;
  gap: 10px;
  min-height: 46px;
  padding-bottom: 12px;
}

.timeline__list li:not(:last-child)::before {
  content: "";
  position: absolute;
  top: 14px;
  bottom: 0;
  left: 5px;
  width: 1px;
  background: var(--line);
}

.timeline__dot {
  width: 11px;
  height: 11px;
  margin-top: 3px;
  border: 2px solid var(--paper);
  border-radius: 50%;
  background: var(--jade);
  box-shadow: 0 0 0 1px var(--jade);
}

.timeline__title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.timeline__title strong {
  font-size: 14px;
}

.timeline__title span {
  color: var(--muted);
  font-size: 11px;
}

.timeline__list p,
.timeline__empty {
  margin: 5px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}

.timeline__empty {
  margin: 0;
  padding: 14px;
  border-radius: 12px;
  background: var(--paper-strong);
}
</style>
