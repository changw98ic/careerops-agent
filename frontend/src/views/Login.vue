<template>
  <div class="login-page">
    <form class="login-card" @submit.prevent="doLogin">
      <h1>CareerOps</h1>
      <p class="subtitle">Login to your account</p>
      <div v-if="error" class="error">{{ error }}</div>
      <label>Username
        <input v-model="username" type="text" autocomplete="username" required />
      </label>
      <label>Password
        <input v-model="password" type="password" autocomplete="current-password" required />
      </label>
      <button type="submit" class="btn-primary" :disabled="loading">
        {{ loading ? 'Logging in...' : 'Login' }}
      </button>
    </form>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { setCsrfToken } from '../api/client.js'
import { setLoggedIn } from '../router.js'

const router = useRouter()
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

onMounted(() => {
  if (document.cookie.includes('careerops_session=')) {
    router.push('/jobs')
  }
})

async function doLogin() {
  loading.value = true
  error.value = ''
  try {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username.value, password: password.value }),
      credentials: 'same-origin',
    })
    const data = await res.json()
    if (data.ok) {
      setCsrfToken(data.csrf_token)
      setLoggedIn(true)
      router.push('/jobs')
    } else {
      error.value = data.error || 'Login failed'
    }
  } catch (e) {
    error.value = 'Network error'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page { display: flex; justify-content: center; align-items: center; min-height: 80vh; }
.login-card { background: #fff; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 360px; }
.login-card h1 { margin-bottom: 4px; }
.subtitle { color: #666; margin-bottom: 24px; font-size: 14px; }
.login-card label { display: block; margin-bottom: 16px; font-size: 14px; font-weight: 500; }
.login-card input { display: block; width: 100%; margin-top: 4px; padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
.login-card button { width: 100%; padding: 10px; margin-top: 8px; font-size: 15px; }
.error { background: #f8d7da; color: #721c24; padding: 8px 12px; border-radius: 4px; margin-bottom: 16px; font-size: 13px; }
</style>
