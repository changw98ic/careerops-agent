<template>
  <a-result status="404" title="404" sub-title="The page you visited does not exist.">
    <template #extra>
      <a-button type="primary" @click="goHome">Back Home</a-button>
    </template>
  </a-result>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { status } from '../stores/session.js'

const router = useRouter()

const goHome = () => {
  const target = status.value === 'authenticated' ? '/dashboard' : '/login'
  router.push(target)
}
</script>
