<template>
  <div class="login-page">
    <div class="login-shell">
      <div class="login-brand">
        <div class="brand-mark">CO</div>
        <span>CareerOps</span>
      </div>

      <a-card class="login-card" :bordered="false">
        <h1>Welcome back</h1>
        <p class="subtitle">Sign in to review your job search workspace.</p>

        <a-alert v-if="error" class="login-error" type="error" show-icon :message="error" />

        <a-form layout="vertical" :model="formData" @finish="doLogin">
          <a-form-item
            label="Username"
            name="username"
            :rules="[{ required: true, message: 'Enter your username.' }]"
          >
            <a-input
              v-model:value="formData.username"
              size="large"
              autocomplete="username"
              placeholder="Enter your username"
              :disabled="loading"
            >
              <template #prefix><UserOutlined /></template>
            </a-input>
          </a-form-item>

          <a-form-item
            label="Password"
            name="password"
            :rules="[{ required: true, message: 'Enter your password.' }]"
          >
            <a-input-password
              v-model:value="formData.password"
              size="large"
              autocomplete="current-password"
              placeholder="Enter your password"
              :disabled="loading"
            >
              <template #prefix><LockOutlined /></template>
            </a-input-password>
          </a-form-item>

          <a-button html-type="submit" type="primary" size="large" block :loading="loading">
            Sign in
          </a-button>
        </a-form>
      </a-card>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
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
const username = ref('')
const password = ref('')
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
  } finally {
    loading.value = false
  }
}
</script>
