<template>
  <div class="login-page" lang="zh-CN">
    <div class="login-shell">
      <div class="login-brand">
        <div class="brand-mark" aria-hidden="true">CO</div>
        <span>CareerOps</span>
      </div>

      <!-- Setup complete: show confirmation, no leaked credentials -->
      <a-card v-if="setupComplete" class="login-card" :bordered="false">
        <h1>账号创建成功</h1>
        <p class="subtitle">你的账号已设置完成，请使用新账号登录。</p>
        <a-button type="primary" size="large" block @click="goToLogin">
          返回登录
        </a-button>
      </a-card>

      <!-- Bootstrap form -->
      <a-card v-else class="login-card" :bordered="false">
        <h1>初始化账号</h1>
        <p class="subtitle">输入引导令牌并创建你的登录凭据。</p>

        <div
          v-if="error"
          id="bootstrap-error"
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
          @finish="doBootstrap"
          :model="formData"
        >
          <a-form-item
            label="引导令牌"
            name="bootstrap_token"
            :rules="[{ required: true, message: '请输入引导令牌。' }]"
          >
            <a-input
              ref="tokenInputRef"
              v-model:value="formData.bootstrap_token"
              size="large"
              placeholder="粘贴一次性令牌"
              :disabled="loading"
              :aria-describedby="error ? 'bootstrap-error' : undefined"
              aria-required="true"
            >
              <template #prefix><KeyOutlined aria-hidden="true" /></template>
            </a-input>
          </a-form-item>

          <a-form-item
            label="用户名"
            name="username"
            :rules="usernameRules"
          >
            <a-input
              ref="usernameInputRef"
              v-model:value="formData.username"
              size="large"
              autocomplete="username"
              placeholder="选择一个用户名"
              :disabled="loading"
              :aria-describedby="error ? 'bootstrap-error' : undefined"
              aria-required="true"
            >
              <template #prefix><UserOutlined aria-hidden="true" /></template>
            </a-input>
          </a-form-item>

          <a-form-item
            label="密码"
            name="password"
            :rules="passwordRules"
          >
            <a-input-password
              ref="passwordInputRef"
              v-model:value="formData.password"
              size="large"
              autocomplete="new-password"
              placeholder="创建密码（至少 12 个字符）"
              :disabled="loading"
              :aria-describedby="error ? 'bootstrap-error' : undefined"
              aria-required="true"
            >
              <template #prefix><LockOutlined aria-hidden="true" /></template>
            </a-input-password>
          </a-form-item>

          <a-form-item
            label="确认密码"
            name="confirm_password"
            :rules="confirmPasswordRules"
          >
            <a-input-password
              ref="confirmInputRef"
              v-model:value="formData.confirm_password"
              size="large"
              autocomplete="new-password"
              placeholder="再次输入密码"
              :disabled="loading"
              :aria-describedby="error ? 'bootstrap-error' : undefined"
              aria-required="true"
            >
              <template #prefix><LockOutlined aria-hidden="true" /></template>
            </a-input-password>
          </a-form-item>

          <a-button
            html-type="submit"
            type="primary"
            size="large"
            block
            :loading="loading"
            :disabled="!formValid"
          >
            创建账号
          </a-button>
        </a-form>
      </a-card>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { KeyOutlined, LockOutlined, UserOutlined } from '@ant-design/icons-vue'
import { bootstrap } from '../stores/session.js'

const router = useRouter()

const formRef = ref(null)
const tokenInputRef = ref(null)
const usernameInputRef = ref(null)
const passwordInputRef = ref(null)
const confirmInputRef = ref(null)

const formData = reactive({
  bootstrap_token: '',
  username: '',
  password: '',
  confirm_password: '',
})

const error = ref('')
const loading = ref(false)
const setupComplete = ref(false)

// -- Validation rules --

const usernameRules = [
  { required: true, message: '请输入用户名。' },
  { min: 3, max: 64, message: '用户名需为 3-64 个字符。' },
  { pattern: /^[a-zA-Z0-9._-]+$/, message: '仅允许字母、数字、点、下划线和连字符。' },
]

const passwordRules = [
  { required: true, message: '请输入密码。' },
  { min: 12, message: '密码至少需要 12 个字符。' },
]

const confirmPasswordRules = [
  { required: true, message: '请确认密码。' },
  {
    validator: (_rule, value) => {
      if (value && value !== formData.password) {
        return Promise.reject(new Error('两次输入的密码不一致。'))
      }
      return Promise.resolve()
    },
  },
]

// -- Form-level validity (for disabling the submit button) --

const formValid = computed(() => {
  const { bootstrap_token, username, password, confirm_password } = formData
  if (!bootstrap_token) return false
  if (username.length < 3 || username.length > 64) return false
  if (!/^[a-zA-Z0-9._-]+$/.test(username)) return false
  if (password.length < 12) return false
  if (confirm_password !== password) return false
  return true
})

// -- Server error code -> user-friendly message --

function mapServerError(code, fallback) {
  switch (code) {
    case 'INVALID_BOOTSTRAP_CREDENTIAL':
      return '引导令牌无效或已过期。'
    case 'BOOTSTRAP_CLOSED':
      return '账号已存在，引导已关闭。'
    case 'RATE_LIMITED':
      return '尝试过于频繁，请稍后再试。'
    case 'VALIDATION_ERROR':
      return '会话丢失，请刷新页面后重试。'
    default:
      return fallback || '引导失败，请重试。'
  }
}

// -- Submit --

async function doBootstrap() {
  loading.value = true
  error.value = ''
  try {
    await bootstrap(formData.bootstrap_token, formData.username, formData.password)
    // Clear all form data to prevent credential leak in DOM/memory.
    formData.bootstrap_token = ''
    formData.username = ''
    formData.password = ''
    formData.confirm_password = ''
    setupComplete.value = true
  } catch (e) {
    error.value = mapServerError(e.code, e.message)
    // Move focus to the first invalid field.
    await nextTick()
    focusFirstInvalid()
  } finally {
    loading.value = false
  }
}

function goToLogin() {
  router.push({ name: 'login' })
}

function focusFirstInvalid() {
  if (!formData.bootstrap_token) {
    const el = tokenInputRef.value?.$el?.querySelector('input')
    if (el) { el.focus(); return }
  }
  if (!formData.username || formData.username.length < 3) {
    const el = usernameInputRef.value?.$el?.querySelector('input')
    if (el) { el.focus(); return }
  }
  if (!formData.password || formData.password.length < 12) {
    const el = passwordInputRef.value?.$el?.querySelector('input')
    if (el) { el.focus(); return }
  }
  if (formData.confirm_password !== formData.password) {
    const el = confirmInputRef.value?.$el?.querySelector('input')
    if (el) { el.focus(); return }
  }
  // Fallback: focus the token field.
  const el = tokenInputRef.value?.$el?.querySelector('input')
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

/* Visible focus indicators */
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
