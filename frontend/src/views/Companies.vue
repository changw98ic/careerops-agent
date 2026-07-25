<template>
  <div>
    <h1>Companies</h1>
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Domains</th>
          <th>Terms Status</th>
          <th>Created</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="c in companies" :key="c.id">
          <td><strong>{{ c.name }}</strong></td>
          <td>{{ (c.official_domains || []).join(', ') }}</td>
          <td><span :class="'badge badge-' + (c.terms_status === 'allowed' ? 'active' : 'favorited')">{{ c.terms_status }}</span></td>
          <td>{{ formatDate(c.created_at) }}</td>
        </tr>
        <tr v-if="!companies.length">
          <td colspan="4" style="text-align:center;color:#999">No companies</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api/client.js'

const companies = ref([])

async function fetchCompanies() {
  try {
    const data = await api.listCompanies()
    companies.value = data.items || []
  } catch (e) {
    console.error(e)
  }
}

function formatDate(d) {
  if (!d) return ''
  return new Date(d).toLocaleDateString('zh-CN')
}

onMounted(fetchCompanies)
</script>
