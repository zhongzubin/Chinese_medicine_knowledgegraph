<script setup lang="ts">
import type {
  AggregateFact,
  AttributeFact,
  Evidence,
  PathEvidence,
} from "@/types/api";

defineProps<{
  evidence: Evidence[];
  facts: AttributeFact[];
  aggregates: AggregateFact[];
  paths: PathEvidence[];
}>();

const relationshipLabels: Record<string, string> = {
  HAS_EFFECT: "具有功效",
  HAS_INGREDIENT: "包含组成",
  TREATS_DISEASE: "治疗疾病",
  ALLEVIATES_SYMPTOM: "缓解症状",
  HAS_SYMPTOM: "表现症状",
  FROM_SOURCE: "来源于",
};

function relationshipLabel(value: string) {
  return relationshipLabels[value] || value;
}
</script>

<template>
  <section
    v-if="evidence.length || facts.length || aggregates.length || paths.length"
    class="evidence surface"
    data-testid="evidence-panel"
    aria-labelledby="evidence-title"
  >
    <div class="evidence__heading">
      <div>
        <span class="eyebrow">Graph evidence</span>
        <h2 id="evidence-title">图谱证据</h2>
      </div>
      <span class="evidence__summary">
        {{ evidence.length + facts.length + aggregates.length + paths.length }} 项
      </span>
    </div>

    <el-tabs class="evidence__tabs" stretch>
      <el-tab-pane :label="`关系 ${evidence.length}`">
        <div v-if="evidence.length" class="evidence__list">
          <article
            v-for="(item, index) in evidence"
            :key="`${item.subject}-${item.relationship}-${item.object}-${index}`"
            class="evidence-card"
          >
            <div class="evidence-card__relation">
              <strong>{{ item.subject }}</strong>
              <span>{{ relationshipLabel(item.relationship) }}</span>
              <strong>{{ item.object }}</strong>
            </div>
            <div class="evidence-card__meta">
              <span>{{ item.subject_label }} → {{ item.object_label }}</span>
              <span :class="{ 'source-missing': !item.source }">
                {{ item.source || "当前图谱未提供出处" }}
              </span>
            </div>
          </article>
        </div>
        <p v-else class="evidence__empty">本轮没有关系证据。</p>
      </el-tab-pane>

      <el-tab-pane :label="`属性 ${facts.length}`">
        <div v-if="facts.length" class="evidence__list">
          <article
            v-for="(fact, index) in facts"
            :key="`${fact.subject}-${fact.attribute}-${index}`"
            class="evidence-card"
          >
            <div class="fact-row">
              <span class="fact-row__label">
                {{ fact.subject }} · {{ fact.attribute_label }}
              </span>
              <strong>{{ fact.value }}</strong>
            </div>
            <div class="evidence-card__meta">
              <span>{{ fact.subject_label }}</span>
              <span :class="{ 'source-missing': !fact.source }">
                {{ fact.source || "当前图谱未提供出处" }}
              </span>
            </div>
          </article>
        </div>
        <p v-else class="evidence__empty">本轮没有属性事实。</p>
      </el-tab-pane>

      <el-tab-pane :label="`统计 ${aggregates.length}`">
        <div v-if="aggregates.length" class="evidence__list">
          <article
            v-for="(aggregate, index) in aggregates"
            :key="`${aggregate.anchor}-${aggregate.relationship}-${index}`"
            class="evidence-card"
          >
            <div class="aggregate-row">
              <strong>{{ aggregate.count }}</strong>
              <div>
                <span>{{ aggregate.description }}</span>
                <small>
                  {{ aggregate.anchor }} · {{ relationshipLabel(aggregate.relationship) }}
                </small>
              </div>
            </div>
          </article>
        </div>
        <p v-else class="evidence__empty">本轮没有受控统计结果。</p>
      </el-tab-pane>

      <el-tab-pane :label="`路径 ${paths.length}`">
        <div v-if="paths.length" class="evidence__list">
          <article
            v-for="(path, index) in paths"
            :key="`${path.root}-${path.target}-${index}`"
            class="evidence-card path-card"
          >
            <div class="path-card__nodes">
              <template v-for="(step, stepIndex) in path.steps" :key="stepIndex">
                <span class="path-card__node">{{ step.subject }}</span>
                <span class="path-card__edge">
                  {{ relationshipLabel(step.relationship) }}
                </span>
              </template>
              <span class="path-card__node">{{ path.target }}</span>
            </div>
            <div class="evidence-card__meta">
              <span>{{ path.root_label }} → {{ path.target_label }}</span>
              <span :class="{ 'source-missing': !path.source }">
                {{ path.source || "当前图谱未提供出处" }}
              </span>
            </div>
          </article>
        </div>
        <p v-else class="evidence__empty">本轮没有受控多跳路径。</p>
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<style scoped>
.evidence {
  padding: 20px 22px 12px;
  border-radius: 18px;
}

.evidence__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

h2 {
  margin: 5px 0 0;
  font-size: 18px;
}

.evidence__summary {
  padding: 5px 9px;
  border-radius: 999px;
  background: var(--paper-strong);
  color: var(--muted);
  font-size: 11px;
}

.evidence__tabs {
  margin-top: 10px;
}

.evidence__list {
  display: grid;
  gap: 10px;
}

.evidence-card {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 13px;
  background: #fffefa;
}

.evidence-card__relation {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 15px;
}

.evidence-card__relation span {
  padding: 3px 8px;
  border-radius: 999px;
  background: var(--jade-soft);
  color: var(--jade-dark);
  font-size: 11px;
}

.evidence-card__meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 9px;
  color: var(--muted);
  font-size: 11px;
}

.fact-row {
  display: grid;
  gap: 5px;
}

.fact-row__label {
  color: var(--muted);
  font-size: 12px;
}

.fact-row strong {
  line-height: 1.65;
}

.aggregate-row {
  display: flex;
  align-items: center;
  gap: 13px;
}

.aggregate-row > strong {
  color: var(--cinnabar);
  font-size: 28px;
}

.aggregate-row div {
  display: grid;
  gap: 3px;
}

.aggregate-row small {
  color: var(--muted);
}

.path-card__nodes {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
}

.path-card__node {
  padding: 6px 9px;
  border-radius: 8px;
  background: var(--jade-soft);
  font-weight: 650;
}

.path-card__edge {
  color: var(--cinnabar);
  font-size: 11px;
}

.evidence__empty {
  margin: 8px 0 16px;
  padding: 16px;
  border-radius: 12px;
  background: var(--paper-strong);
  color: var(--muted);
  font-size: 13px;
}

:deep(.el-tabs__item) {
  color: var(--muted);
}

:deep(.el-tabs__item.is-active) {
  color: var(--jade-dark);
}

:deep(.el-tabs__active-bar) {
  background: var(--jade);
}

@media (max-width: 640px) {
  .evidence {
    padding: 16px 16px 8px;
  }

  .evidence-card__meta {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }
}
</style>
