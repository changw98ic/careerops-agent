<!--
  SystemSendStatusBadge — renders the lifecycle of one system-managed send
  (Section 10, task 10.11).

  Iron rule: a queued / pending send is NEVER displayed as "sent". Only a
  confirmed provider receipt (phase === 'sent') may render a success state.
  Ambiguous outcomes surface as reconciliation-required (warning), never as
  success or as a silent retry.
-->
<template>
  <div class="system-send-status">
    <a-tag :color="phaseColor">
      <component :is="phaseIcon" v-if="phaseIcon" />
      <span class="system-send-status__label">{{ phaseLabel }}</span>
    </a-tag>
    <span v-if="status.provider_resource_id" class="system-send-status__ref mono">
      {{ shortId(status.provider_resource_id) }}
    </span>
    <a-tooltip v-if="status.denial_reasons && status.denial_reasons.length" :title="denialText">
      <InfoCircleOutlined class="system-send-status__info" />
    </a-tooltip>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  ExclamationCircleOutlined,
  InfoCircleOutlined,
  CloseCircleOutlined,
} from '@ant-design/icons-vue'

const props = defineProps({
  // { phase: 'pending'|'sent'|'failed'|'reconciliation_required',
  //   provider_resource_id?, denial_reasons?, submitted_at? }
  status: { type: Object, required: true },
})

// phase -> { color, label, icon }. ``pending`` is deliberately "处理中"
// (in progress) — it must never read as "已发送" (sent).
const PHASE_META = {
  pending: { color: 'processing', label: '发送处理中', icon: ClockCircleOutlined },
  sent: { color: 'success', label: '已发送', icon: CheckCircleOutlined },
  failed: { color: 'error', label: '发送失败', icon: CloseCircleOutlined },
  reconciliation_required: {
    color: 'warning',
    label: '需要人工核对',
    icon: ExclamationCircleOutlined,
  },
}

const phase = computed(() => props.status?.phase || 'pending')
const meta = computed(() => PHASE_META[phase.value] || PHASE_META.pending)
const phaseColor = computed(() => meta.value.color)
const phaseLabel = computed(() => meta.value.label)
const phaseIcon = computed(() => meta.value.icon)
const denialText = computed(() =>
  (props.status?.denial_reasons || []).join('，')
)

function shortId(value) {
  if (!value) return ''
  return String(value).slice(0, 8)
}
</script>

<style scoped>
.system-send-status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.system-send-status__ref {
  font-size: 12px;
  opacity: 0.7;
}
.system-send-status__info {
  color: var(--ant-color-text-secondary, rgba(0, 0, 0, 0.45));
  cursor: help;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
</style>
