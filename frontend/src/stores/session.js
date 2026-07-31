import { reactive } from 'vue'

// Task-14 stub: the console login backend is gone, so this store no longer
// talks to /api/v1/auth/* (login/bootstrap/preauth/logout/checkSession were
// removed with the login UI). Only `user.candidate_id` is still consumed —
// AgentWorkbench gates matching/agent actions on it. Task 15 replaces this
// store with a candidate store.
export const user = reactive({
  candidate_id: '',
})
