<template>
  <div class="dashboard-chart" :class="`dashboard-chart-${type}`">
    <svg
      class="dashboard-chart-svg"
      viewBox="0 0 640 280"
      role="img"
      :aria-label="chartLabel"
      preserveAspectRatio="none"
    >
      <template v-if="type === 'status'">
        <line class="chart-axis" :x1="plot.left" :y1="plot.top + plot.height" :x2="plot.left + plot.width" :y2="plot.top + plot.height" />
        <line class="chart-axis" :x1="plot.left" :y1="plot.top" :x2="plot.left" :y2="plot.top + plot.height" />
        <g v-for="bar in statusBars" :key="bar.state">
          <rect
            class="chart-mark chart-mark-interactive"
            :x="bar.x"
            :y="bar.y"
            :width="bar.width"
            :height="bar.height"
            :fill="bar.color"
            rx="6"
            tabindex="0"
            role="button"
            :aria-label="`${bar.label} ${bar.count} 个职位`"
            @click="select(bar)"
            @keydown="selectWithKeyboard($event, bar)"
          >
            <title>{{ bar.label }}：{{ bar.count }}</title>
          </rect>
          <text class="chart-label" :x="bar.x + bar.width / 2" :y="plot.top + plot.height + 24" text-anchor="middle">
            {{ bar.label }}
          </text>
        </g>
        <text class="chart-scale-label" :x="plot.left - 10" :y="plot.top + 5" text-anchor="end">{{ maxValue }}</text>
        <text class="chart-scale-label" :x="plot.left - 10" :y="plot.top + plot.height + 4" text-anchor="end">0</text>
      </template>

      <template v-else-if="type === 'trend'">
        <line class="chart-axis" :x1="plot.left" :y1="plot.top + plot.height" :x2="plot.left + plot.width" :y2="plot.top + plot.height" />
        <line class="chart-gridline" :x1="plot.left" :y1="plot.top" :x2="plot.left + plot.width" :y2="plot.top" />
        <polygon class="chart-area" :points="trendAreaPoints" />
        <polyline class="chart-line" :points="trendPoints" />
        <g v-for="point in trendMarks" :key="point.date">
          <circle
            class="chart-mark chart-mark-interactive"
            :cx="point.x"
            :cy="point.y"
            r="5"
            tabindex="0"
            role="button"
            :aria-label="`${point.date} 新增 ${point.count} 个职位`"
            @click="select(point)"
            @keydown="selectWithKeyboard($event, point)"
          >
            <title>{{ point.date }}：新增 {{ point.count }} 个职位</title>
          </circle>
        </g>
        <text v-for="label in trendLabels" :key="label.date" class="chart-label" :x="label.x" :y="plot.top + plot.height + 24" text-anchor="middle">
          {{ label.text }}
        </text>
        <text class="chart-scale-label" :x="plot.left - 10" :y="plot.top + 5" text-anchor="end">{{ maxValue }}</text>
        <text class="chart-scale-label" :x="plot.left - 10" :y="plot.top + plot.height + 4" text-anchor="end">0</text>
      </template>

      <template v-else-if="type === 'funnel'">
        <g v-for="bar in funnelBars" :key="bar.state">
          <text class="chart-label chart-funnel-label" :x="plot.left" :y="bar.y + 16">{{ bar.state }}</text>
          <rect class="chart-funnel-track" :x="plot.left" :y="bar.y + 25" :width="plot.width" height="20" rx="10" />
          <rect
            class="chart-mark chart-mark-interactive"
            :x="plot.left"
            :y="bar.y + 25"
            :width="bar.width"
            height="20"
            :fill="bar.color"
            rx="10"
            tabindex="0"
            role="button"
            :aria-label="`${bar.state} ${bar.count} 条投递`"
            @click="select(bar)"
            @keydown="selectWithKeyboard($event, bar)"
          >
            <title>{{ bar.state }}：{{ bar.count }}</title>
          </rect>
          <text class="chart-value-label" :x="plot.left + plot.width + 10" :y="bar.y + 41">{{ bar.count }}</text>
        </g>
      </template>

      <template v-else-if="type === 'timeline'">
        <line class="chart-timeline-axis" :x1="plot.left" :y1="plot.top + plot.height / 2" :x2="plot.left + plot.width" :y2="plot.top + plot.height / 2" />
        <g v-for="point in timelinePoints" :key="point.id">
          <line class="chart-timeline-tick" :x1="point.x" :y1="plot.top + plot.height / 2 - 18" :x2="point.x" :y2="plot.top + plot.height / 2 + 18" />
          <circle
            class="chart-mark chart-mark-interactive"
            :cx="point.x"
            :cy="plot.top + plot.height / 2"
            r="8"
            :fill="point.color"
            tabindex="0"
            role="button"
            :aria-label="`${point.date}，${point.state}`"
            @click="select(point)"
            @keydown="selectWithKeyboard($event, point)"
          >
            <title>{{ point.date }}：{{ point.state }}</title>
          </circle>
          <text class="chart-label" :x="point.x" :y="plot.top + plot.height / 2 - 30" text-anchor="middle">{{ point.state }}</text>
          <text class="chart-scale-label" :x="point.x" :y="plot.top + plot.height / 2 + 42" text-anchor="middle">{{ point.date.slice(5) }}</text>
        </g>
      </template>
    </svg>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  type: { type: String, required: true },
  data: { type: Array, default: () => [] },
})

const emit = defineEmits(['select'])

const plot = { left: 52, top: 18, width: 540, height: 202 }
const palette = ['#ea580c', '#f59e0b', '#16a34a', '#2563eb', '#8b5cf6', '#ec4899', '#06b6d4']

const chartLabel = computed(() => {
  const labels = {
    status: '职位状态分布图',
    trend: '近三十天职位新增趋势图',
    funnel: '投递状态分布图',
    timeline: '近期投递时间线图',
  }
  return labels[props.type] || '数据图表'
})

const maxValue = computed(() => {
  const values = props.data.map((item) => Number(item.count) || 0)
  return Math.max(1, ...values)
})

const statusBars = computed(() => {
  const gap = 18
  const width = Math.max(24, (plot.width - gap * Math.max(props.data.length - 1, 0)) / Math.max(props.data.length, 1))
  return props.data.map((item, index) => {
    const count = Number(item.count) || 0
    const height = (count / maxValue.value) * (plot.height - 8)
    return {
      ...item,
      label: item.state,
      x: plot.left + index * (width + gap),
      y: plot.top + plot.height - height,
      width,
      height,
      color: palette[index % palette.length],
    }
  })
})

function pointFor(item, index, length = props.data.length) {
  const x = plot.left + (length <= 1 ? plot.width / 2 : (index / (length - 1)) * plot.width)
  const count = Number(item.count) || 0
  const y = plot.top + plot.height - (count / maxValue.value) * (plot.height - 8)
  return { ...item, x, y, count }
}

const trendMarks = computed(() => props.data.map((item, index) => pointFor(item, index)))
const trendPoints = computed(() => trendMarks.value.map((point) => `${point.x},${point.y}`).join(' '))
const trendAreaPoints = computed(() => {
  if (!trendMarks.value.length) return ''
  const first = trendMarks.value[0]
  const last = trendMarks.value[trendMarks.value.length - 1]
  return `${first.x},${plot.top + plot.height} ${trendPoints.value} ${last.x},${plot.top + plot.height}`
})
const trendLabels = computed(() => {
  if (!trendMarks.value.length) return []
  const indexes = [...new Set([0, Math.floor((trendMarks.value.length - 1) / 2), trendMarks.value.length - 1])]
  return indexes.map((index) => ({
    date: trendMarks.value[index].date,
    text: trendMarks.value[index].date.slice(5),
    x: trendMarks.value[index].x,
  }))
})

const funnelBars = computed(() => {
  const gap = 18
  const rowHeight = Math.max(46, (plot.height - gap * Math.max(props.data.length - 1, 0)) / Math.max(props.data.length, 1))
  return props.data.map((item, index) => ({
    ...item,
    y: plot.top + index * (rowHeight + gap),
    width: (Number(item.count) || 0) / maxValue.value * plot.width,
    color: palette[index % palette.length],
  }))
})

const timelinePoints = computed(() => props.data.map((item, index) => ({
  ...item,
  id: item.id || `${item.date}-${index}`,
  x: plot.left + (props.data.length <= 1 ? plot.width / 2 : (index / (props.data.length - 1)) * plot.width),
  color: palette[index % palette.length],
})))

function select(item) {
  emit('select', item)
}

function selectWithKeyboard(event, item) {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    select(item)
  }
}
</script>

<style scoped>
.dashboard-chart {
  width: 100%;
  height: 280px;
}

.dashboard-chart-svg {
  display: block;
  width: 100%;
  height: 100%;
  overflow: visible;
}

.chart-axis,
.chart-timeline-axis {
  stroke: rgba(28, 25, 23, 0.12);
  stroke-width: 1;
}

.chart-gridline {
  stroke: rgba(28, 25, 23, 0.08);
  stroke-dasharray: 4 5;
}

.chart-label,
.chart-scale-label,
.chart-value-label {
  fill: #78716c;
  font-family: 'Geist', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 12px;
}

.chart-scale-label {
  fill: #a8a29e;
  font-size: 11px;
}

.chart-value-label {
  fill: #1c1917;
  font-weight: 650;
}

.chart-mark-interactive {
  cursor: pointer;
  outline: none;
  transition: opacity 160ms ease, filter 160ms ease;
}

.chart-mark-interactive:hover,
.chart-mark-interactive:focus-visible {
  opacity: 0.78;
  filter: brightness(0.92);
}

.chart-line {
  fill: none;
  stroke: #2563eb;
  stroke-width: 3;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.chart-area {
  fill: rgba(37, 99, 235, 0.1);
}

.dashboard-chart-trend .chart-mark-interactive {
  fill: #ffffff;
  stroke: #2563eb;
  stroke-width: 2.5;
}

.chart-funnel-label {
  font-weight: 600;
}

.chart-funnel-track {
  fill: rgba(28, 25, 23, 0.06);
}

.chart-timeline-tick {
  stroke: rgba(37, 99, 235, 0.22);
  stroke-width: 2;
}

@media (prefers-reduced-motion: reduce) {
  .chart-mark-interactive {
    transition: none;
  }
}
</style>
