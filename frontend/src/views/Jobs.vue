<template>
  <div>
    <h1>Jobs Inbox</h1>
    <div class="toolbar">
      <input v-model="search" placeholder="Search title..." @input="fetchJobs" />
      <select v-model="stateFilter" @change="fetchJobs">
        <option value="">All States</option>
        <option value="active">Active</option>
        <option value="closed">Closed</option>
      </select>
      <span class="count">{{ total }} jobs</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Title</th>
          <th>State</th>
          <th>Created</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="job in jobs" :key="job.id">
          <td>{{ job.canonical_title }}</td>
          <td><span :class="'badge badge-' + job.aggregate_state">{{ job.aggregate_state }}</span></td>
          <td>{{ formatDate(job.created_at) }}</td>
          <td>
            <router-link :to="'/jobs/' + job.id" class="btn-primary btn-sm" style="text-decoration:none">Detail</router-link>
          </td>
        </tr>
        <tr v-if="!jobs.length">
          <td colspan="4" style="text-align:center;color:#999">No jobs found</td>
        </tr>
      </tbody>
    </table>
    <div v-if="loading" class="loading">Loading...</div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api/client.js'

const jobs = ref([])
const total = ref(0)
const search = ref('')
const stateFilter = ref('')
const loading = ref(false)

async function fetchJobs() {
  loading.value = true
  try {
    const params = {}
    if (search.value) params.q = search.value
    if (stateFilter.value) params.state = stateFilter.value
    const data = await api.listJobs(params)
    jobs.value = data.items || []
    total.value = data.total || 0
  } catch (e) {
    console.error('Failed to fetch jobs:', e)
  } finally {
    loading.value = false
  }
}

function formatDate(d) {
  if (!d) return ''
  return new Date(d).toLocaleDateString('zh-CN')
}

onMounted(fetchJobs)
</script>

<style scoped>
.count { color: #666; font-size: 13px; margin-left: auto; }
.loading { text-align: center; padding: 20px; color: #666; }
</style>
