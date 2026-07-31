import { ref } from 'vue'

/**
 * Candidate store (auth-rm Task 15).
 *
 * The console login is gone, so the frontend needs an explicit way to pick
 * which candidate's environment every per-candidate API call addresses.
 * This store owns that identity:
 *
 * - `candidates`            — the global candidate list (GET /api/v1/candidates)
 * - `currentCandidateId`    — the selected candidate id, persisted to
 *                             localStorage so a refresh keeps the selection
 * - `status`                — 'idle' | 'loading' | 'ready' | 'empty' | 'error'
 * - `load()`                — fetch the list; keep the stored id when it still
 *                             exists, otherwise fall back to the first entry
 * - `switchCandidate(id)`   — select another candidate (persists)
 * - `create(name)`          — POST a new candidate, then reload + select it
 *
 * The API client imports `currentCandidateId` to build per-candidate URLs
 * (`/api/v1/candidates/{cid}/...`). This store deliberately uses raw fetch
 * for its own two endpoints (list/create) instead of the api client, so the
 * two modules never import each other in a cycle.
 */

const STORAGE_KEY = 'careerops.current_candidate_id'

function readStoredId() {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || ''
  } catch {
    // localStorage unavailable (SSR / privacy mode) — start without a
    // selection; load() will default to the first candidate.
    return ''
  }
}

function persistId(id) {
  try {
    if (id) {
      window.localStorage.setItem(STORAGE_KEY, id)
    } else {
      window.localStorage.removeItem(STORAGE_KEY)
    }
  } catch {
    // Best-effort persistence; the in-memory selection still works.
  }
}

export const candidates = ref([])
export const currentCandidateId = ref(readStoredId())
export const status = ref('idle')

function listFromData(data) {
  return Array.isArray(data?.items) ? data.items : []
}

/**
 * Fetch the global candidate list and settle `currentCandidateId`.
 * Keeps the persisted selection when it still exists in the list; otherwise
 * falls back to the first candidate; with zero candidates the store enters
 * the `empty` state and the UI shows the create prompt.
 * Re-throws so the caller can surface the error.
 */
export async function load() {
  status.value = 'loading'
  try {
    const res = await fetch('/api/v1/candidates', { credentials: 'same-origin' })
    if (!res.ok) {
      const err = new Error(`加载候选人列表失败（HTTP ${res.status}）`)
      err.status = res.status
      throw err
    }
    const data = await res.json()
    candidates.value = listFromData(data)
    const stored = currentCandidateId.value
    const storedStillExists = stored && candidates.value.some((c) => String(c.id) === stored)
    if (!storedStillExists) {
      currentCandidateId.value = candidates.value[0]?.id ? String(candidates.value[0].id) : ''
    }
    persistId(currentCandidateId.value)
    status.value = candidates.value.length ? 'ready' : 'empty'
    return candidates.value
  } catch (err) {
    status.value = 'error'
    throw err
  }
}

/** Select another candidate (persists the choice to localStorage). */
export function switchCandidate(id) {
  const next = String(id || '')
  if (next === currentCandidateId.value) return
  currentCandidateId.value = next
  persistId(next)
}

/**
 * Create a candidate and make it the current one.
 * POSTs `{display_name}` (the global candidates CRUD route), then reloads
 * the list so the new entry appears; the created candidate is selected.
 * Re-throws so the caller can surface the error.
 */
export async function create(displayName) {
  const name = String(displayName || '').trim()
  if (!name) throw new Error('候选人名称不能为空')
  const res = await fetch('/api/v1/candidates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ display_name: name }),
    credentials: 'same-origin',
  })
  if (!res.ok) {
    const err = new Error(`创建候选人失败（HTTP ${res.status}）`)
    err.status = res.status
    throw err
  }
  const created = await res.json()
  if (created?.id) {
    currentCandidateId.value = String(created.id)
    persistId(currentCandidateId.value)
  }
  return load()
}
