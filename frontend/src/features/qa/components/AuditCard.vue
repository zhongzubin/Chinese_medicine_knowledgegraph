<script setup lang="ts">
import type { AnswerVerification } from "@/types/api";

defineProps<{
  verification: AnswerVerification | null;
}>();
</script>

<template>
  <section
    v-if="verification"
    class="audit surface"
    data-testid="audit-card"
    aria-labelledby="audit-title"
  >
    <div class="audit__heading">
      <div>
        <span class="eyebrow">Evidence audit</span>
        <h2 id="audit-title">回答校验</h2>
      </div>
      <el-tag
        :type="verification.status === 'passed' ? 'success' : 'warning'"
        effect="dark"
      >
        {{ verification.status === "passed" ? "逐项通过" : "已证据化回退" }}
      </el-tag>
    </div>

    <div class="audit__metrics">
      <div>
        <strong>{{ verification.matched_items }}/{{ verification.checked_items }}</strong>
        <span>证据覆盖</span>
      </div>
      <div>
        <strong>{{ verification.evidence_items }}</strong>
        <span>关系证据</span>
      </div>
      <div>
        <strong>{{ verification.fact_items }}</strong>
        <span>属性事实</span>
      </div>
      <div>
        <strong>{{ verification.missing_sources }}</strong>
        <span>未提供出处</span>
      </div>
    </div>
    <p>{{ verification.message }}</p>
  </section>
</template>

<style scoped>
.audit {
  padding: 20px;
  border-radius: 18px;
}

.audit__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

h2 {
  margin: 5px 0 0;
  font-size: 18px;
}

.audit__metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin: 16px 0 12px;
}

.audit__metrics div {
  padding: 12px;
  border-radius: 12px;
  background: var(--jade-soft);
}

.audit__metrics strong,
.audit__metrics span {
  display: block;
}

.audit__metrics strong {
  color: var(--jade-dark);
  font-size: 20px;
}

.audit__metrics span {
  margin-top: 3px;
  color: var(--muted);
  font-size: 11px;
}

p {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.7;
}

@media (max-width: 640px) {
  .audit__metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
