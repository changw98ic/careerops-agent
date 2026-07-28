<template>
  <div v-if="visible" class="candidate-setup-notice" role="status">
    <div class="candidate-setup-copy">
      <strong>先完成职业画像</strong>
      <span>这项功能需要候选人的画像版本和确认后的资料作为输入。</span>
    </div>
    <router-link to="/profile">
      <a-button type="primary" size="small">去创建职业画像</a-button>
    </router-link>
  </div>
</template>

<script setup>
defineProps({
  visible: { type: Boolean, default: false },
})
</script>

<style scoped>
.candidate-setup-notice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin: 16px 0;
  padding: 14px 16px;
  border: 1px solid #f2c36b;
  border-radius: 10px;
  background: #fff8e6;
  color: #7a4d00;
}

.candidate-setup-copy {
  display: grid;
  gap: 4px;
}

.candidate-setup-copy span {
  color: #8c6b2f;
  font-size: 13px;
}

@media (max-width: 640px) {
  .candidate-setup-notice {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
