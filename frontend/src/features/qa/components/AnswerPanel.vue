<script setup lang="ts">
defineProps<{
  answer: string;
  grounded: boolean;
  busy: boolean;
}>();
</script>

<template>
  <section class="answer surface" aria-labelledby="answer-title">
    <div class="answer__heading">
      <div>
        <span class="eyebrow">Answer</span>
        <h2 id="answer-title">证据化回答</h2>
      </div>
      <el-tag
        v-if="answer"
        :type="grounded ? 'success' : 'warning'"
        effect="plain"
      >
        {{ grounded ? "图谱证据支持" : "未形成图谱结论" }}
      </el-tag>
    </div>

    <div v-if="answer" class="answer__body" data-testid="answer-body" aria-live="polite">
      {{ answer }}
    </div>
    <div v-else-if="busy" class="answer__loading" aria-live="polite">
      <span></span>
      <p>正在检索并审核本轮证据…</p>
    </div>
    <p v-else class="answer__empty">
      回答会在这里出现。系统不会用图谱之外的猜测补全医疗事实。
    </p>
  </section>
</template>

<style scoped>
.answer {
  padding: 22px;
  border-radius: 18px;
}

.answer__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

h2 {
  margin: 5px 0 0;
  font-size: 20px;
}

.answer__body {
  margin-top: 18px;
  color: #263a34;
  font-size: 16px;
  line-height: 1.9;
  white-space: pre-wrap;
}

.answer__empty {
  margin: 16px 0 0;
  padding: 18px;
  border-radius: 12px;
  background: var(--paper-strong);
  color: var(--muted);
  line-height: 1.7;
}

.answer__loading {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 18px;
  color: var(--muted);
}

.answer__loading span {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  background: var(--jade);
  animation: pulse 1.1s ease-in-out infinite;
}

@keyframes pulse {
  50% {
    opacity: 0.35;
    transform: scale(0.75);
  }
}
</style>
