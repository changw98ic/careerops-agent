<template>
  <div class="candidate-selector">
    <template v-if="candidates.length">
      <a-select
        class="candidate-dropdown"
        :value="currentCandidateId"
        :loading="status === 'loading'"
        placeholder="选择候选人"
        aria-label="当前候选人"
        @change="onChange"
      >
        <a-select-option v-for="c in candidates" :key="c.id" :value="String(c.id)">
          {{ c.display_name || c.id }}
        </a-select-option>
      </a-select>
      <a-button class="candidate-add-btn" @click="openCreate">＋ 新建候选人</a-button>
    </template>
    <template v-else-if="status === 'ready' || status === 'empty'">
      <a-button type="primary" @click="openCreate">创建候选人</a-button>
      <span class="candidate-hint">还没有候选人，创建后即可开始使用。</span>
    </template>
    <template v-else-if="status === 'error'">
      <a-button @click="retryLoad">重新加载候选人</a-button>
    </template>
    <a-spin v-else size="small" />

    <a-modal
      v-model:open="createOpen"
      title="新建候选人"
      :confirm-loading="creating"
      @ok="submitCreate"
      @cancel="closeCreate"
    >
      <a-form layout="vertical">
        <a-form-item label="候选人名称" required>
          <a-input
            v-model:value="newName"
            placeholder="例如：张伟"
            :disabled="creating"
            @press-enter="submitCreate"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  candidates,
  currentCandidateId,
  status,
  load as loadCandidates,
  switchCandidate,
  create as createCandidate,
} from '../stores/candidate.js'

const createOpen = ref(false)
const creating = ref(false)
const newName = ref('')

function onChange(id) {
  if (id) switchCandidate(id)
}

async function retryLoad() {
  try {
    await loadCandidates()
  } catch (err) {
    message.error(err.message || '加载候选人列表失败，请重试。')
  }
}

function openCreate() {
  newName.value = ''
  createOpen.value = true
}

function closeCreate() {
  if (creating.value) return
  createOpen.value = false
}

async function submitCreate() {
  const name = newName.value.trim()
  if (!name) {
    message.warning('请输入候选人名称。')
    return
  }
  creating.value = true
  try {
    await createCandidate(name)
    createOpen.value = false
    message.success(`候选人「${name}」已创建并切换。`)
  } catch (err) {
    message.error(err.message || '创建候选人失败，请稍后重试。')
  } finally {
    creating.value = false
  }
}
</script>

<style scoped>
.candidate-selector {
  display: flex;
  align-items: center;
  gap: 8px;
}

.candidate-dropdown {
  min-width: 160px;
  max-width: 240px;
}

.candidate-add-btn {
  flex-shrink: 0;
}

.candidate-hint {
  font-size: 13px;
  color: var(--color-tertiary);
}
</style>
