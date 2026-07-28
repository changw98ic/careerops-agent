<template>
  <div class="skeleton-loader" :class="[`skeleton-${variant}`, { 'skeleton-animated': animated }]">
    <!-- 文本骨架 -->
    <template v-if="variant === 'text'">
      <div
        v-for="i in lines"
        :key="i"
        class="skeleton-line"
        :style="{ width: i === lines ? '60%' : '100%' }"
      />
    </template>

    <!-- 卡片骨架 -->
    <template v-else-if="variant === 'card'">
      <div class="skeleton-card">
        <div class="skeleton-header">
          <div class="skeleton-avatar" />
          <div class="skeleton-meta">
            <div class="skeleton-line" style="width: 40%" />
            <div class="skeleton-line" style="width: 60%" />
          </div>
        </div>
        <div class="skeleton-body">
          <div class="skeleton-line" />
          <div class="skeleton-line" />
          <div class="skeleton-line" style="width: 80%" />
        </div>
      </div>
    </template>

    <!-- 统计卡片骨架 -->
    <template v-else-if="variant === 'stat'">
      <div class="skeleton-stat">
        <div class="skeleton-icon" />
        <div class="skeleton-content">
          <div class="skeleton-label" />
          <div class="skeleton-value" />
        </div>
      </div>
    </template>

    <!-- 图表骨架 -->
    <template v-else-if="variant === 'chart'">
      <div class="skeleton-chart">
        <div class="skeleton-chart-header">
          <div class="skeleton-line" style="width: 30%" />
          <div class="skeleton-line" style="width: 20%" />
        </div>
        <div class="skeleton-chart-body">
          <div v-for="i in 5" :key="i" class="skeleton-bar" :style="{ height: `${20 + Math.random() * 60}%` }" />
        </div>
      </div>
    </template>

    <!-- 自定义骨架 -->
    <template v-else>
      <slot />
    </template>
  </div>
</template>

<script setup>
defineProps({
  variant: {
    type: String,
    default: 'text',
    validator: (value) => ['text', 'card', 'stat', 'chart', 'custom'].includes(value)
  },
  lines: {
    type: Number,
    default: 3
  },
  animated: {
    type: Boolean,
    default: true
  }
})
</script>

<style scoped>
.skeleton-loader {
  width: 100%;
}

/* 基础动画 */
.skeleton-animated .skeleton-line,
.skeleton-animated .skeleton-avatar,
.skeleton-animated .skeleton-icon,
.skeleton-animated .skeleton-label,
.skeleton-animated .skeleton-value,
.skeleton-animated .skeleton-bar {
  background: linear-gradient(
    90deg,
    var(--surface-tertiary) 25%,
    var(--surface-secondary) 50%,
    var(--surface-tertiary) 75%
  );
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.5s ease-in-out infinite;
}

@keyframes skeleton-shimmer {
  0% {
    background-position: 200% 0;
  }
  100% {
    background-position: -200% 0;
  }
}

/* 文本骨架 */
.skeleton-line {
  height: 14px;
  border-radius: var(--radius-xs);
  margin-bottom: 12px;
  background: var(--surface-tertiary);
}

.skeleton-line:last-child {
  margin-bottom: 0;
}

/* 卡片骨架 */
.skeleton-card {
  padding: 20px;
  border-radius: var(--radius-md);
  background: var(--surface-primary);
  border: 1px solid rgba(0, 0, 0, 0.06);
}

.skeleton-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.skeleton-avatar {
  width: 48px;
  height: 48px;
  border-radius: var(--radius-sm);
  background: var(--surface-tertiary);
  flex-shrink: 0;
}

.skeleton-meta {
  flex: 1;
}

.skeleton-meta .skeleton-line {
  height: 12px;
}

.skeleton-meta .skeleton-line:first-child {
  height: 14px;
  margin-bottom: 8px;
}

.skeleton-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* 统计卡片骨架 */
.skeleton-stat {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 24px;
  border-radius: var(--radius-md);
  background: var(--surface-primary);
  border: 1px solid rgba(0, 0, 0, 0.06);
}

.skeleton-icon {
  width: 48px;
  height: 48px;
  border-radius: var(--radius-md);
  background: var(--surface-tertiary);
  flex-shrink: 0;
}

.skeleton-content {
  flex: 1;
}

.skeleton-label {
  height: 12px;
  width: 60%;
  border-radius: var(--radius-xs);
  background: var(--surface-tertiary);
  margin-bottom: 8px;
}

.skeleton-value {
  height: 28px;
  width: 40%;
  border-radius: var(--radius-xs);
  background: var(--surface-tertiary);
}

/* 图表骨架 */
.skeleton-chart {
  padding: 20px;
  border-radius: var(--radius-md);
  background: var(--surface-primary);
  border: 1px solid rgba(0, 0, 0, 0.06);
}

.skeleton-chart-header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 20px;
}

.skeleton-chart-body {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  height: 200px;
  padding-top: 20px;
}

.skeleton-bar {
  flex: 1;
  border-radius: var(--radius-xs) var(--radius-xs) 0 0;
  background: var(--surface-tertiary);
  min-height: 20px;
}

/* 减少动画支持 */
@media (prefers-reduced-motion: reduce) {
  .skeleton-animated .skeleton-line,
  .skeleton-animated .skeleton-avatar,
  .skeleton-animated .skeleton-icon,
  .skeleton-animated .skeleton-label,
  .skeleton-animated .skeleton-value,
  .skeleton-animated .skeleton-bar {
    animation: none;
  }
}
</style>
