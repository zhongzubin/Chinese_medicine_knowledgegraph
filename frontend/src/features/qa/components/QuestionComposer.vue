<script setup lang="ts">
import { ref } from "vue";

const props = defineProps<{
  busy: boolean;
}>();

const emit = defineEmits<{
  submit: [question: string];
  cancel: [];
}>();

const draft = ref("");

function submit() {
  const question = draft.value.trim();
  if (!question || props.busy) {
    return;
  }
  emit("submit", question);
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    submit();
  }
}
</script>

<template>
  <section class="composer surface" aria-labelledby="composer-title">
    <div class="composer__heading">
      <div>
        <span id="composer-title" class="eyebrow">Ask with evidence</span>
        <h2>提出问题，查看图谱依据</h2>
      </div>
      <span class="composer__hint">Ctrl / ⌘ + Enter 发送</span>
    </div>

    <el-input
      v-model="draft"
      data-testid="question-input"
      class="composer__input"
      type="textarea"
      :rows="3"
      maxlength="500"
      show-word-limit
      resize="none"
      placeholder="例如：麻黄有什么功效？图谱中是否有来源？"
      :disabled="busy"
      @keydown="handleKeydown"
    />

    <div class="composer__actions">
      <span class="composer__privacy">
        匿名会话仅保存最近 5 轮，不保存模型隐藏推理。
      </span>
      <div class="composer__buttons">
        <el-button v-if="busy" data-testid="cancel-question" @click="emit('cancel')">
          停止等待
        </el-button>
        <el-button
          type="primary"
          data-testid="submit-question"
          :disabled="busy || !draft.trim()"
          @click="submit"
        >
          {{ busy ? "正在检索证据" : "发送问题" }}
        </el-button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.composer {
  padding: 22px;
  border-radius: 18px;
}

.composer__heading,
.composer__actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.composer__heading {
  margin-bottom: 14px;
}

h2 {
  margin: 5px 0 0;
  font-size: 20px;
  letter-spacing: -0.02em;
}

.composer__hint,
.composer__privacy {
  color: var(--muted);
  font-size: 12px;
}

.composer__input {
  margin-bottom: 14px;
}

.composer__buttons {
  display: flex;
  gap: 10px;
}

:deep(.el-textarea__inner) {
  min-height: 92px !important;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: #fffefa;
  box-shadow: none;
  padding: 14px 15px;
  line-height: 1.7;
}

:deep(.el-textarea__inner:focus) {
  border-color: var(--jade);
  box-shadow: 0 0 0 3px rgba(23, 107, 91, 0.1);
}

:deep(.el-button--primary) {
  --el-button-bg-color: var(--jade);
  --el-button-border-color: var(--jade);
  --el-button-hover-bg-color: var(--jade-dark);
  --el-button-hover-border-color: var(--jade-dark);
}

@media (max-width: 640px) {
  .composer {
    padding: 16px;
  }

  .composer__heading,
  .composer__actions {
    align-items: flex-start;
    flex-direction: column;
  }

  .composer__hint,
  .composer__privacy {
    display: none;
  }

  .composer__buttons {
    width: 100%;
  }

  .composer__buttons :deep(.el-button) {
    flex: 1;
  }
}
</style>
