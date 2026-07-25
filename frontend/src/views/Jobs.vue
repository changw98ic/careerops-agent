<template>
  <div>
    <h1>Jobs Inbox</h1>
    <div class="toolbar">
      <input v-model="search" placeholder="Search title..." @input="debouncedFetch" />
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
          <th>Location</th>
          <th>State</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="job in jobs" :key="job.id">
          <td>
            <div class="job-title">{{ job.canonical_title }}</div>
            <div class="job-desc" v-if="job._location || job._description">
              {{ job._description ? job._description.slice(0, 120) + '...' : '' }}
            </div>
          </td>
          <td>{{ job._location || '-' }}</td>
          <td><span :class="'badge badge-' + job.aggregate_state">{{ job.aggregate_state }}</span></td>
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
let debounceTimer = null

function debouncedFetch() {
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(fetchJobs, 300)
}

async function fetchJobs() {
  loading.value = true
  try {
    const params = {}
    if (search.value) params.q = search.value
    if (stateFilter.value) params.state = stateFilter.value
    const data = await api.listJobs(params)
    const items = data.items || []
    // Fetch detail for each job to get location/description
    const details = await Promise.all(items.map(j => api.getJob(j.id).catch(() => null)))
    items.forEach((job, i) => {
      const detail = details[i]
      if (detail && detail.versions && detail.versions.length) {
        const latest = detail.versions[0]
        const sd = latest.structured_data || {}
        job._location = sd.location || ''
        job._description = sd.description || ''
      }
    })
    jobs.value = items
    total.value = data.total || 0
  } catch (e) {
    console.error('Failed to fetch jobs:', e)
  } finally {
    loading.value = false
  }
}

onMounted(fetchJobs)
</script>

<style scoped>
.count { color: #666; font-size: 13px; margin-left: auto; }
.loading { text-align: center; padding: 20px; color: #666; }
.job-title { font-weight: 500; }
.job-desc { font-size: 12px; color: #888; margin-top: 4px; max-width: 500px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
