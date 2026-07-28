<template>
  <nav
    v-if="steps.length > 0"
    class="first-run-guide"
    aria-label="首次使用引导"
    lang="zh-CN"
  >
    <p class="guide-heading">快速上手：完成以下步骤解锁完整功能</p>
    <ol class="guide-steps">
      <li
        v-for="step in steps"
        :key="step.key"
        class="guide-step"
        :class="{ 'guide-step--done': step.done }"
      >
        <span class="step-status" aria-hidden="true">
          <template v-if="step.done">&#10003;</template>
          <template v-else>{{ step.index }}</template>
        </span>
        <div class="step-body">
          <p class="step-title">{{ step.title }}</p>
          <p class="step-desc">{{ step.description }}</p>
        </div>
        <router-link
          v-if="!step.done"
          :to="step.to"
          class="step-action"
        >
          {{ step.cta }}
        </router-link>
        <span v-else class="step-done-label" aria-label="已完成">已完成</span>
      </li>
    </ol>
  </nav>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  profileDone: { type: Boolean, default: false },
  resumeDone: { type: Boolean, default: false },
  evidenceDone: { type: Boolean, default: false },
  crawlPlanDone: { type: Boolean, default: false },
})

const STEP_DEFS = [
  {
    key: 'profile',
    title: '完善个人档案',
    description: '填写基本信息和求职意向，让系统了解你的背景。',
    cta: '前往个人档案',
    to: { name: 'profile' },
    doneKey: 'profileDone',
  },
  {
    key: 'resume',
    title: '上传并确认简历',
    description: '上传简历后系统会自动解析，请确认提取的信息无误。',
    cta: '前往简历管理',
    to: { name: 'resumes' },
    doneKey: 'resumeDone',
  },
  {
    key: 'evidence',
    title: '确认证明材料',
    description: '审核系统提取的技能和经历证据，确认后用于匹配和审查。',
    cta: '前往证明材料',
    to: { name: 'evidence' },
    doneKey: 'evidenceDone',
  },
  {
    key: 'crawlPlan',
    title: '配置爬取计划',
    description: '设置职位来源和爬取频率，让系统持续发现新职位。',
    cta: '前往爬取计划',
    to: { name: 'crawl-plans' },
    doneKey: 'crawlPlanDone',
  },
]

const steps = computed(() =>
  STEP_DEFS.map((def, i) => ({
    key: def.key,
    index: i + 1,
    title: def.title,
    description: def.description,
    cta: def.cta,
    to: def.to,
    done: props[def.doneKey],
  })).filter((s) => !s.done || s.key !== 'crawlPlan'),
)
</script>

<style scoped>
.first-run-guide {
  padding: 16px 18px;
  border: 1px solid rgba(0, 0, 0, 0.08);
  border-radius: 8px;
  background: var(--surface-primary, #fff);
  margin-top: 12px;
}

.guide-heading {
  margin: 0 0 14px;
  font-size: 14px;
  font-weight: 600;
  color: var(--color-primary, rgba(0, 0, 0, 0.88));
}

.guide-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 12px;
}

.guide-step {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid rgba(0, 0, 0, 0.06);
  border-radius: 6px;
  background: var(--surface-secondary, #fafafa);
}

.guide-step--done {
  opacity: 0.6;
}

.step-status {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  background: #1677ff;
  color: #fff;
}

.guide-step--done .step-status {
  background: #52c41a;
}

.step-body {
  flex: 1;
  min-width: 0;
}

.step-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--color-primary, rgba(0, 0, 0, 0.88));
}

.step-desc {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--color-tertiary, rgba(0, 0, 0, 0.45));
  line-height: 1.5;
}

.step-action {
  flex-shrink: 0;
  padding: 4px 12px;
  border: 1px solid #1677ff;
  border-radius: 4px;
  background: #1677ff;
  color: #fff;
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
}

.step-action:hover {
  background: #4096ff;
  border-color: #4096ff;
}

.step-action:focus-visible {
  outline: 2px solid #1677ff;
  outline-offset: 2px;
}

.step-done-label {
  flex-shrink: 0;
  font-size: 12px;
  color: #52c41a;
  font-weight: 600;
}

/* Non-color state: done steps get a checkmark AND reduced opacity,
   not just green color */
.guide-step--done .step-title {
  text-decoration: line-through;
  text-decoration-color: rgba(0, 0, 0, 0.25);
}

/* Reduced motion */
@media (prefers-reduced-motion: reduce) {
  .step-action {
    transition: none;
  }
}

/* 200% zoom support: steps stack vertically */
@media (max-width: 640px) {
  .guide-step {
    flex-wrap: wrap;
  }

  .step-action {
    width: 100%;
    text-align: center;
    margin-top: 6px;
  }
}
</style>
