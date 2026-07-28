<template>
  <aside
    class="model-disabled-notice"
    role="status"
    aria-live="polite"
    lang="zh-CN"
  >
    <div class="notice-icon" aria-hidden="true">
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="10" cy="10" r="9" stroke="currentColor" stroke-width="1.5" />
        <path d="M10 6v5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
        <circle cx="10" cy="14" r="1" fill="currentColor" />
      </svg>
    </div>
    <div class="notice-body">
      <p class="notice-title">当前未启用大模型</p>
      <p class="notice-desc">
        <template v-if="context === 'workbench'">
          模型能力由后端发布开关决定；关闭时只返回安全降级结果，不会伪装成模型结论。您可以使用确定性匹配功能，它基于规则和已确认的证据运行。
        </template>
        <template v-else>
          智能输入功能需要大模型支持。请改用手动填写方式完成操作。
        </template>
      </p>
      <a-button
        v-if="context === 'workbench'"
        type="default"
        size="small"
        class="notice-action"
        @click="$emit('navigate', 'matching')"
      >
        前往确定性匹配
      </a-button>
    </div>
  </aside>
</template>

<script setup>
defineProps({
  /**
   * Context where this notice is displayed.
   * "workbench" = Agent workbench page
   * "intake"    = Smart intake surfaces
   */
  context: {
    type: String,
    default: 'workbench',
    validator: (v) => ['workbench', 'intake'].includes(v),
  },
})

defineEmits(['navigate'])
</script>

<style scoped>
.model-disabled-notice {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 14px 16px;
  border: 1px solid #faad14;
  border-left: 4px solid #faad14;
  border-radius: 6px;
  background: #fffbe6;
  margin-top: 8px;
}

/* Non-color state indicator: left border stripe alongside the warning color */
.notice-icon {
  flex-shrink: 0;
  color: #d48806;
  margin-top: 2px;
}

.notice-body {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.notice-title {
  margin: 0;
  font-weight: 600;
  font-size: 14px;
  color: rgba(0, 0, 0, 0.88);
}

.notice-desc {
  margin: 0;
  font-size: 13px;
  color: rgba(0, 0, 0, 0.65);
  line-height: 1.6;
}

.notice-action {
  justify-self: start;
  margin-top: 4px;
}

/* Visible focus indicator */
.notice-action:focus-visible {
  outline: 2px solid #1677ff;
  outline-offset: 2px;
}

/* Reduced motion */
@media (prefers-reduced-motion: reduce) {
  .model-disabled-notice {
    transition: none;
  }
}
</style>
