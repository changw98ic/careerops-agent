<template>
  <div v-if="visible" class="performance-monitor">
    <div class="monitor-header">
      <span class="monitor-title">性能监控</span>
      <button class="monitor-close" @click="visible = false">
        <CloseOutlined />
      </button>
    </div>

    <div class="monitor-content">
      <!-- 加载指标 -->
      <div class="metric-group">
        <div class="metric-label">页面加载</div>
        <div class="metric-value">{{ metrics.loadTime }}ms</div>
      </div>

      <!-- DOM 指标 -->
      <div class="metric-group">
        <div class="metric-label">DOM 解析</div>
        <div class="metric-value">{{ metrics.domParseTime }}ms</div>
      </div>

      <!-- 资源指标 -->
      <div class="metric-group">
        <div class="metric-label">资源数量</div>
        <div class="metric-value">{{ metrics.resourceCount }}</div>
      </div>

      <!-- 内存指标 -->
      <div v-if="metrics.memoryUsage" class="metric-group">
        <div class="metric-label">内存使用</div>
        <div class="metric-value" :class="{ 'metric-warning': metrics.memoryUsage > 80 }">
          {{ metrics.memoryUsage }}%
        </div>
      </div>

      <!-- FPS 指标 -->
      <div class="metric-group">
        <div class="metric-label">FPS</div>
        <div class="metric-value" :class="{ 'metric-warning': metrics.fps < 30 }">
          {{ metrics.fps }}
        </div>
      </div>
    </div>

    <div class="monitor-footer">
      <button class="monitor-btn" @click="refreshMetrics">
        <ReloadOutlined />
        <span>刷新</span>
      </button>
      <button class="monitor-btn" @click="exportReport">
        <DownloadOutlined />
        <span>导出</span>
      </button>
    </div>
  </div>

  <!-- 触发按钮 -->
  <button
    v-else
    class="performance-trigger"
    :class="{ 'trigger-warning': hasWarning }"
    @click="visible = true"
  >
    <DashboardOutlined />
  </button>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import {
  CloseOutlined,
  ReloadOutlined,
  DownloadOutlined,
  DashboardOutlined,
} from '@ant-design/icons-vue'
import {
  collectLoadMetrics,
  collectResourceMetrics,
  getPerformanceReport,
} from '@/utils/performance'

const visible = ref(false)
const hasWarning = ref(false)

const metrics = reactive({
  loadTime: 0,
  domParseTime: 0,
  resourceCount: 0,
  memoryUsage: null,
  fps: 60,
})

let fpsFrames = 0
let fpsTime = performance.now()
let fpsAnimationId = null

/**
 * 刷新性能指标
 */
function refreshMetrics() {
  // 加载指标
  const loadMetrics = collectLoadMetrics()
  if (loadMetrics) {
    metrics.loadTime = Math.round(loadMetrics.loadComplete || 0)
    metrics.domParseTime = Math.round(loadMetrics.domParse || 0)
  }

  // 资源指标
  const resources = collectResourceMetrics()
  metrics.resourceCount = resources?.length || 0

  // 内存指标
  if (performance.memory) {
    const memory = performance.memory
    metrics.memoryUsage = Math.round(
      (memory.usedJSHeapSize / memory.jsHeapSizeLimit) * 100
    )
    hasWarning.value = metrics.memoryUsage > 80
  }
}

/**
 * 计算 FPS
 */
function calculateFPS() {
  fpsFrames++
  const now = performance.now()
  if (now - fpsTime >= 1000) {
    metrics.fps = Math.round((fpsFrames * 1000) / (now - fpsTime))
    fpsFrames = 0
    fpsTime = now
    hasWarning.value = metrics.fps < 30 || metrics.memoryUsage > 80
  }
  fpsAnimationId = requestAnimationFrame(calculateFPS)
}

/**
 * 导出性能报告
 */
function exportReport() {
  const report = getPerformanceReport()
  const blob = new Blob([JSON.stringify(report, null, 2)], {
    type: 'application/json',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `performance-report-${Date.now()}.json`
  a.click()
  URL.revokeObjectURL(url)
}

// 生命周期
onMounted(() => {
  refreshMetrics()
  calculateFPS()
})

onUnmounted(() => {
  if (fpsAnimationId) {
    cancelAnimationFrame(fpsAnimationId)
  }
})
</script>

<style scoped>
.performance-monitor {
  position: fixed;
  bottom: 20px;
  right: 20px;
  width: 280px;
  background: var(--surface-primary);
  border: 1px solid rgba(0, 0, 0, 0.1);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 9999;
  overflow: hidden;
}

.monitor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--surface-secondary);
  border-bottom: 1px solid rgba(0, 0, 0, 0.06);
}

.monitor-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-primary);
}

.monitor-close {
  width: 24px;
  height: 24px;
  border: none;
  background: transparent;
  color: var(--color-tertiary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-xs);
  transition: all 0.2s ease;
}

.monitor-close:hover {
  background: rgba(0, 0, 0, 0.06);
  color: var(--color-primary);
}

.monitor-content {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.metric-group {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.metric-label {
  font-size: 13px;
  color: var(--color-tertiary);
}

.metric-value {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-primary);
  font-family: 'Geist Mono', monospace;
}

.metric-value.metric-warning {
  color: var(--color-warning);
}

.monitor-footer {
  display: flex;
  gap: 8px;
  padding: 12px 16px;
  background: var(--surface-secondary);
  border-top: 1px solid rgba(0, 0, 0, 0.06);
}

.monitor-btn {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 12px;
  border: 1px solid rgba(0, 0, 0, 0.1);
  border-radius: var(--radius-sm);
  background: var(--surface-primary);
  color: var(--color-primary);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.monitor-btn:hover {
  border-color: var(--accent-primary);
  color: var(--accent-primary);
}

.performance-trigger {
  position: fixed;
  bottom: 20px;
  right: 20px;
  width: 40px;
  height: 40px;
  border: none;
  background: var(--accent-primary);
  color: var(--color-inverse);
  border-radius: 50%;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  box-shadow: var(--shadow-md);
  transition: all 0.2s ease;
  z-index: 9999;
}

.performance-trigger:hover {
  transform: scale(1.1);
}

.performance-trigger.trigger-warning {
  background: var(--color-warning);
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% {
    box-shadow: 0 0 0 0 rgba(180, 83, 9, 0.4);
  }
  50% {
    box-shadow: 0 0 0 10px rgba(180, 83, 9, 0);
  }
}

/* 仅在开发环境显示 */
@media (min-width: 1px) {
  .performance-monitor,
  .performance-trigger {
    display: none;
  }
}

/* 开发环境显示 */
.performance-monitor,
.performance-trigger {
  display: flex;
}
</style>
