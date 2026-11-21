<script setup lang="ts">
defineProps<{
  errorCode: string;
  errorMessage: string;
  warnings: string[];
  degraded: boolean;
}>();
</script>

<template>
  <section
    v-if="errorMessage || warnings.length"
    class="status-stack"
    aria-live="assertive"
  >
    <el-alert
      v-if="errorMessage"
      data-testid="qa-error"
      :title="errorMessage"
      :description="errorCode"
      type="error"
      show-icon
      :closable="false"
    />
    <el-alert
      v-for="warning in warnings"
      :key="warning"
      :title="warning"
      :type="degraded ? 'warning' : 'info'"
      show-icon
      :closable="false"
    />
  </section>
</template>

<style scoped>
.status-stack {
  display: grid;
  gap: 10px;
}

:deep(.el-alert) {
  border-radius: 12px;
}
</style>
