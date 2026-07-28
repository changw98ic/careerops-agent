<template>
  <div class="login-page" lang="zh-CN">
    <div class="login-shell">
      <div class="login-brand">
        <div class="brand-mark" aria-hidden="true">CO</div>
        <span>CareerOps</span>
      </div>

      <a-card class="login-card" :bordered="false">
        <h1>欢迎回来</h1>
        <p class="subtitle">登录以查看你的求职工作区。</p>

        <div
          v-if="error"
          id="login-error"
          class="login-error"
          role="alert"
          aria-live="assertive"
        >
          <span aria-hidden="true" class="error-icon">!</span>
          <span>{{ error }}</span>
        </div>

        <a-form
          ref="formRef"
          layout="vertical"
          :model="formData"
          @finish="doLogin"
        >
          <a-form-item
            label="用户名"
            name="username"
            :rules="[{ required: true, message: '请输入用户名。' }]"
          >
            <a-input
              ref="usernameInputRef"
              v-model:value="formData.username"
              size="large"
              autocomplete="username"
              placeholder="请输入用户名"
              :disabled="loading"
              :aria-describedby="error ? 'login-error' : undefined"
              aria-required="true"
            >
              <template #prefix><UserOutlined aria-hidden="true" /></template>
            </a-input>
          </a-form-item>

          <a-form-item
            label="密码"
            name="password"
            :rules="[{ required: true, message: '请输入密码。' }]"
          >
            <a-input-password
              ref="passwordInputRef"
              v-model:value="formData.password"
              size="large"
              autocomplete="current-password"
              placeholder="请输入密码"
              :disabled="loading"
              :aria-describedby="error ? 'login-error' : undefined"
              aria-required="true"
            >
              <template #prefix><LockOutlined aria-hidden="true" /></template>
            </a-input-password>
          </a-form-item>

          <a-button html-type="submit" type="primary" size="large" block :loading="loading">
            登录
          </a-button>
        </a-form>
      </a-card>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { LockOutlined, UserOutlined } from '@ant-design/icons-vue'
import { login } from '../stores/session.js'

const ERROR_MAP = {
  'Username or password is invalid': '用户名或密码错误',
  'Too many attempts; try again later': '登录尝试过于频繁，请稍后再试',
  'Authentication service is not available': '认证服务未配置',
  'Session is invalid or expired': '登录会话已过期，请重试',
  'Missing preauth session; load the login page first': '登录会话已过期，请重试',
  'Preauth failed': '登录会话初始化失败，请重试',
}

const router = useRouter()
const formRef = ref(null)
const usernameInputRef = ref(null)
const passwordInputRef = ref(null)
const formData = reactive({ username: '', password: '' })
const error = ref('')
const loading = ref(false)

async function doLogin() {
  loading.value = true
  error.value = ''
  try {
    await login(formData.username, formData.password)
    await router.push({ name: 'dashboard' })
  } catch (e) {
    error.value = ERROR_MAP[e.message] || '登录失败，请重试'
    // Move focus to the first invalid field after error.
    await nextTick()
    focusFirstInvalid()
  } finally {
    loading.value = false
  }
}

function focusFirstInvalid() {
  if (!formData.username) {
    const el = usernameInputRef.value?.$el?.querySelector('input')
    if (el) { el.focus(); return }
  }
  if (!formData.password) {
    const el = passwordInputRef.value?.$el?.querySelector('input')
    if (el) { el.focus(); return }
  }
  // If both fields have values but server rejected, focus username.
  const el = usernameInputRef.value?.$el?.querySelector('input')
  if (el) el.focus()
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--surface-secondary, #f5f5f5);
  padding: 24px;
}

.login-shell {
  width: 100%;
  max-width: 400px;
}

.login-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 24px;
  justify-content: center;
}

.brand-mark {
  width: 40px;
  height: 40px;
  border-radius: 8px;
  background: #1677ff;
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 16px;
}

.login-card {
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}

.login-card h1 {
  margin: 0 0 4px;
  font-size: 22px;
  font-weight: 700;
  color: var(--color-primary, rgba(0, 0, 0, 0.88));
}

.subtitle {
  margin: 0 0 20px;
  font-size: 14px;
  color: var(--color-tertiary, rgba(0, 0, 0, 0.45));
}

.login-error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border: 1px solid #ff4d4f;
  border-left: 4px solid #ff4d4f;
  border-radius: 6px;
  background: #fff2f0;
  color: #cf1322;
  font-size: 14px;
  margin-bottom: 16px;
}

.error-icon {
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #ff4d4f;
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
}

/* Visible focus indicators for keyboard navigation */
:deep(.ant-input:focus-visible),
:deep(.ant-input-password:focus-visible) {
  border-color: #1677ff;
  outline: 2px solid #1677ff;
  outline-offset: 2px;
  box-shadow: none;
}

:deep(.ant-btn:focus-visible) {
  outline: 2px solid #1677ff;
  outline-offset: 2px;
}

/* Reduced motion */
@media (prefers-reduced-motion: reduce) {
  :deep(.ant-btn),
  :deep(.ant-input),
  :deep(.ant-input-password) {
    transition: none;
  }
}

/* 200% zoom support */
@media (max-width: 480px) {
  .login-page {
    padding: 12px;
  }
}
</style>
