<template>
  <div v-if="job">
    <div class="toolbar">
      <router-link to="/jobs" style="text-decoration:none">&larr; Back</router-link>
      <h1 style="margin:0">{{ job.canonical_title }}</h1>
      <button class="btn-success" @click="apply" :disabled="applying" v-if="!applied">
        {{ applying ? 'Applying...' : 'Apply' }}
      </button>
      <span v-else class="badge badge-submitted">Applied</span>
    </div>
    <div class="detail-grid">
      <div class="card">
        <h3>Info</h3>
        <p><strong>State:</strong> {{ job.aggregate_state }}</p>
        <p><strong>Created:</strong> {{ formatDate(job.created_at) }}</p>
      </div>
      <div class="card" v-if="job.versions && job.versions.length">
        <h3>Versions ({{ job.versions.length }})</h3>
        <div v-for="v in job.versions" :key="v.id" class="version-item">
          <span class="version-label">{{ v.parser_version }}</span>
          <span class="version-date">{{ formatDate(v.created_at) }}</span>
        </div>
      </div>
      <div class="card" v-if="job.structured_data">
        <h3>Description</h3>
        <div class="desc">{{ job.structured_data.description || 'No description' }}</div>
      </div>
    </div>
  </div>
  <div v-else-if="loading" class="loading">Loading...</div>
  <div v-else class="loading">Job not found</div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client.js'

const route = useRoute()
const job = ref(null)
const loading = ref(true)
const applying = ref(false)
const applied = ref(false)

async function fetchJob() {
  try {
    job.value = await api.getJob(route.params.id)
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

async function apply() {
  applying.value = true
  try {
    await api.createApplication({
      canonical_job_id: route.params.id,
      apply_url: '',
      note: '',
    })
    applied.value = true
  } catch (e) {
    alert('Apply failed: ' + e.message)
  } finally {
    applying.value = false
  }
}

function formatDate(d) {
  if (!d) return ''
  return new Date(d).toLocaleDateString('zh-CN')
}

onMounted(fetchJob)
</script>

<style scoped>
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.card { background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.card h3 { margin-bottom: 12px; font-size: 15px; color: #666; }
.card p { margin-bottom: 6px; font-size: 14px; }
.desc { font-size: 14px; line-height: 1.6; max-height: 400px; overflow-y: auto; }
.version-item { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; border-bottom: 1px solid #f0f0f0; }
.version-label { font-weight: 500; }
.version-date { color: #999; }
.loading { text-align: center; padding: 40px; color: #666; }
</style>
