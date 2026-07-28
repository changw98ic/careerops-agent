<template>
  <div v-if="hasError" class="error-boundary">
    <div class="error-content">
      <ExclamationCircleOutlined class="error-icon" />
      <h3 class="error-title">{{ title }}</h3>
      <p class="error-message">{{ message }}</p>
      <button v-if="retryable" class="retry-btn" @click="handleRetry">
        <ReloadOutlined />
        <span>重试</span>
      </button>
    </div>
  </div>
  <slot v-else />
</template>

<script setup>
import { ref, onErrorCaptured } from 'vue'
import { ExclamationCircleOutlined, ReloadOutlined } from '@ant-design/icons-vue'

const props = defineProps({
  title: {
    type: String,
    default: '组件加载失败'
  },
  message: {
    type: String,
    default: '发生了一个错误，请稍后重试'
  },
  retryable: {
    type: Boolean,
    default: true
  }
})

const emit = defineEmits(['retry'])

const hasError = ref(false)
const error = ref(null)

onErrorCaptured((err, instance, info) => {
  hasError.value = true
  error.value = err
  console.error('[ErrorBoundary]', err, info)
  return false // 阻止错误继续传播
})

function handleRetry() {
  hasError.value = false
  error.value = null
  emit('retry')
}
</script>

<style scoped>
.error-boundary {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 200px;
  padding: 24px;
}

.error-content {
  text-align: center;
  max-width: 400px;
}

.error-icon {
  font-size: 48px;
  color: var(--color-error);
  margin-bottom: 16px;
}

.error-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--color-primary);
  margin: 0 0 8px 0;
}

.error-message {
  font-size: 14px;
  color: var(--color-tertiary);
  margin: 0 0 24px 0;
  line-height: 1.5;
}

.retry-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 10px 20px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--accent-primary);
  background: transparent;
  color: var(--accent-primary);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
}

.retry-btn:hover {
  background: var(--accent-primary);
  color: var(--color-inverse);
}
</style>
