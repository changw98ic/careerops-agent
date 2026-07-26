import { ref, reactive } from 'vue'
import { setCsrfToken } from '../api/client.js'

// Session status: unknown | checking | anonymous | authenticated
export const status = ref('unknown')

export const user = reactive({
  username: '',
  candidate_id: '',
  csrf_token: '',
  expires_at: null,
})

/**
 * Check current session with the backend.
 * Call on app startup to restore session from HttpOnly cookie.
 * @returns {Promise<string>} the resolved session status
 */
export async function checkSession() {
  status.value = 'checking'
  try {
    const res = await fetch('/api/v1/auth/session', {
      credentials: 'same-origin',
    })
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`)
    }
    const data = await res.json()
    if (data.authenticated && data.user) {
      user.username = data.user.username || ''
      user.candidate_id = data.user.candidate_id || ''
      user.csrf_token = data.csrf_token || ''
      user.expires_at = data.expires_at || null
      setCsrfToken(user.csrf_token)
      status.value = 'authenticated'
    } else {
      clearUser()
      status.value = 'anonymous'
    }
  } catch {
    clearUser()
    status.value = 'anonymous'
  }
  return status.value
}

/**
 * Obtain a short-lived preauth session cookie.
 * Must be called before login() or bootstrap().
 * Returns the preauth CSRF token.
 */
async function ensurePreauth() {
  const res = await fetch('/api/v1/auth/preauth', {
    method: 'POST',
    credentials: 'same-origin',
  })
  const data = await res.json()
  if (!data.ok) {
    throw new Error(data.error || 'Preauth failed')
  }
  // Store the preauth CSRF token — the server also sets it as a cookie,
  // but we keep it in memory so the login/bootstrap request body can
  // reference it if needed.
  setCsrfToken(data.csrf_token || '')
  return data.csrf_token
}

/**
 * Log in with username and password.
 * On success updates session state and CSRF token.
 * On failure throws an error with the server message.
 */
export async function login(username, password) {
  await ensurePreauth()
  const res = await fetch('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
    credentials: 'same-origin',
  })
  const data = await res.json()
  if (!data.ok) {
    throw new Error(data.error || 'Login failed')
  }
  user.username = username
  user.csrf_token = data.csrf_token || ''
  user.expires_at = data.expires_at || null
  setCsrfToken(user.csrf_token)
  status.value = 'authenticated'
}

/**
 * Bootstrap the first admin user with a one-time token.
 * On success updates session state and CSRF token (same as login).
 * On failure throws an error with the server message and code.
 */
export async function bootstrap(bootstrapToken, username, password) {
  await ensurePreauth()
  const res = await fetch('/api/v1/auth/bootstrap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      bootstrap_token: bootstrapToken,
      username,
      password,
    }),
    credentials: 'same-origin',
  })
  const data = await res.json()
  if (!data.ok) {
    const err = new Error(data.error || 'Bootstrap failed')
    err.code = data.code || null
    throw err
  }
  user.username = username
  user.csrf_token = data.csrf_token || ''
  user.expires_at = data.expires_at || null
  setCsrfToken(user.csrf_token)
  status.value = 'authenticated'
}

/**
 * Log out: clear server session, local state, and redirect to login.
 */
export async function logout() {
  try {
    await fetch('/api/v1/auth/logout', {
      method: 'POST',
      headers: { 'X-CSRF-Token': user.csrf_token },
      credentials: 'same-origin',
    })
  } catch {
    // Best-effort logout; clear local state regardless
  }
  clearUser()
  status.value = 'anonymous'
  setCsrfToken('')
  window.location.href = '/login'
}

export function clearUser() {
  user.username = ''
  user.candidate_id = ''
  user.csrf_token = ''
  user.expires_at = null
}
