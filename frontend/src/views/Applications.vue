<template>
  <div>
    <h1>Applications</h1>
    <div class="toolbar">
      <select v-model="stateFilter" @change="fetchApps">
        <option value="">All States</option>
        <option value="favorited">Favorited</option>
        <option value="preparing">Preparing</option>
        <option value="submitted">Submitted</option>
        <option value="ignored">Ignored</option>
      </select>
      <span class="count">{{ apps.length }} applications</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Job ID</th>
          <th>State</th>
          <th>Apply URL</th>
          <th>Submitted</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="a in apps" :key="a.id">
          <td><router-link :to="'/jobs/' + a.canonical_job_id">{{ a.canonical_job_id.slice(0, 8) }}...</router-link></td>
          <td><span :class="'badge badge-' + a.state">{{ a.state }}</span></td>
          <td>{{ a.apply_url || '-' }}</td>
          <td>{{ a.submitted_at ? formatDate(a.submitted_at) : '-' }}</td>
          <td>
            <button v-if="a.state === 'favorited'" class="btn-primary btn-sm" @click="submit(a.id)">Submit</button>
            <button v-if="a.state === 'preparing'" class="btn-success btn-sm" @click="submit(a.id)">Submit</button>
          </td>
        </tr>
        <tr v-if="!apps.length">
          <td colspan="5" style="text-align:center;color:#999">No applications</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api/client.js'

const apps = ref([])
const stateFilter = ref('')

async function fetchApps() {
  try {
    const params = {}
    if (stateFilter.value) params.state = stateFilter.value
    const data = await api.listApplications(params)
    apps.value = data.items || []
  } catch (e) {
    console.error(e)
  }
}

async function submit(id) {
  try {
    await api.submitApplication(id)
    await fetchApps()
  } catch (e) {
    alert('Submit failed: ' + e.message)
  }
}

function formatDate(d) {
  if (!d) return ''
  return new Date(d).toLocaleDateString('zh-CN')
}

onMounted(fetchApps)
</script>

<style scoped>
.count { color: #666; font-size: 13px; margin-left: auto; }
</style>
