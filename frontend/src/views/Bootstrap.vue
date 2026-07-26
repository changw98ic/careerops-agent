<template>
  <div class="login-page">
    <div class="login-shell">
      <div class="login-brand">
        <div class="brand-mark">CO</div>
        <span>CareerOps</span>
      </div>

      <a-card class="login-card" :bordered="false">
        <h1>Set up your account</h1>
        <p class="subtitle">Enter the bootstrap token and create your credentials.</p>

        <a-alert v-if="error" class="login-error" type="error" show-icon :message="error" />

        <a-form layout="vertical" @finish="doBootstrap" :model="formData">
          <a-form-item
            label="Bootstrap Token"
            name="bootstrap_token"
            :rules="[{ required: true, message: 'Enter your bootstrap token.' }]"
          >
            <a-input
              v-model:value="formData.bootstrap_token"
              size="large"
              placeholder="Paste your one-time token"
              :disabled="loading"
            >
              <template #prefix><KeyOutlined /></template>
            </a-input>
          </a-form-item>

          <a-form-item
            label="Username"
            name="username"
            :rules="usernameRules"
          >
            <a-input
              v-model:value="formData.username"
              size="large"
              autocomplete="username"
              placeholder="Choose a username"
              :disabled="loading"
            >
              <template #prefix><UserOutlined /></template>
            </a-input>
          </a-form-item>

          <a-form-item
            label="Password"
            name="password"
            :rules="passwordRules"
          >
            <a-input-password
              v-model:value="formData.password"
              size="large"
              autocomplete="new-password"
              placeholder="Create a password (min 12 characters)"
              :disabled="loading"
            >
              <template #prefix><LockOutlined /></template>
            </a-input-password>
          </a-form-item>

          <a-form-item
            label="Confirm Password"
            name="confirm_password"
            :rules="confirmPasswordRules"
          >
            <a-input-password
              v-model:value="formData.confirm_password"
              size="large"
              autocomplete="new-password"
              placeholder="Confirm your password"
              :disabled="loading"
            >
              <template #prefix><LockOutlined /></template>
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
            Create account
          </a-button>
        </a-form>
      </a-card>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { KeyOutlined, LockOutlined, UserOutlined } from '@ant-design/icons-vue'
import { bootstrap } from '../stores/session.js'

const router = useRouter()

const formData = reactive({
  bootstrap_token: '',
  username: '',
  password: '',
  confirm_password: '',
})

const error = ref('')
const loading = ref(false)

// -- Validation rules --

const usernameRules = [
  { required: true, message: 'Enter a username.' },
  { min: 3, max: 64, message: 'Username must be 3-64 characters.' },
  { pattern: /^[a-zA-Z0-9._-]+$/, message: 'Only letters, numbers, dots, underscores, and hyphens.' },
]

const passwordRules = [
  { required: true, message: 'Enter a password.' },
  { min: 12, message: 'Password must be at least 12 characters.' },
]

const confirmPasswordRules = [
  { required: true, message: 'Confirm your password.' },
  {
    validator: (_rule, value) => {
      if (value && value !== formData.password) {
        return Promise.reject(new Error('Passwords do not match.'))
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
      return 'Bootstrap token is invalid or expired.'
    case 'BOOTSTRAP_CLOSED':
      return 'An account already exists. Bootstrap is closed.'
    case 'RATE_LIMITED':
      return 'Too many attempts. Please try again later.'
    case 'VALIDATION_ERROR':
      return 'Missing session. Reload the page and try again.'
    default:
      return fallback || 'Bootstrap failed.'
  }
}

// -- Submit --

async function doBootstrap() {
  loading.value = true
  error.value = ''
  try {
    await bootstrap(formData.bootstrap_token, formData.username, formData.password)
    await router.push({ name: 'dashboard' })
  } catch (e) {
    error.value = mapServerError(e.code, e.message)
  } finally {
    loading.value = false
  }
}
</script>
