<template>
  <div
    ref="containerRef"
    class="virtual-list"
    :style="{ height: `${height}px`, overflow: 'auto' }"
    @scroll="handleScroll"
  >
    <!-- 占位空间 -->
    <div
      class="virtual-list-spacer"
      :style="{ height: `${totalHeight}px`, position: 'relative' }"
    >
      <!-- 可见项目 -->
      <div
        v-for="item in visibleItems"
        :key="item.index"
        class="virtual-list-item"
        :style="{
          position: 'absolute',
          top: `${item.top}px`,
          left: 0,
          right: 0,
          height: `${itemHeight}px`
        }"
      >
        <slot :item="item.data" :index="item.index" />
      </div>
    </div>

    <!-- 加载更多指示器 -->
    <div v-if="loading" class="virtual-list-loading">
      <slot name="loading">
        <div class="loading-spinner">
          <ReloadOutlined spin />
          <span>加载中...</span>
        </div>
      </slot>
    </div>

    <!-- 空状态 -->
    <div v-if="!loading && items.length === 0" class="virtual-list-empty">
      <slot name="empty">
        <div class="empty-state">
          <InboxOutlined />
          <p>暂无数据</p>
        </div>
      </slot>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { ReloadOutlined, InboxOutlined } from '@ant-design/icons-vue'

const props = defineProps({
  items: {
    type: Array,
    required: true,
    default: () => []
  },
  height: {
    type: Number,
    default: 400
  },
  itemHeight: {
    type: Number,
    default: 50
  },
  overscan: {
    type: Number,
    default: 5
  },
  loading: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['load-more', 'scroll'])

const containerRef = ref(null)
const scrollTop = ref(0)

// 计算总高度
const totalHeight = computed(() => {
  return props.items.length * props.itemHeight
})

// 计算可见范围
const visibleRange = computed(() => {
  const start = Math.floor(scrollTop.value / props.itemHeight)
  const visibleCount = Math.ceil(props.height / props.itemHeight)
  const overscanStart = Math.max(0, start - props.overscan)
  const overscanEnd = Math.min(props.items.length, start + visibleCount + props.overscan)

  return {
    start: overscanStart,
    end: overscanEnd
  }
})

// 计算可见项目
const visibleItems = computed(() => {
  const { start, end } = visibleRange.value
  const items = []

  for (let i = start; i < end; i++) {
    if (i < props.items.length) {
      items.push({
        index: i,
        data: props.items[i],
        top: i * props.itemHeight
      })
    }
  }

  return items
})

// 处理滚动事件
function handleScroll(event) {
  const target = event.target
  scrollTop.value = target.scrollTop

  // 检查是否需要加载更多
  const { scrollHeight, clientHeight, scrollTop: currentScrollTop } = target
  const threshold = 100 // 距离底部 100px 时触发加载

  if (scrollHeight - clientHeight - currentScrollTop < threshold) {
    emit('load-more')
  }

  emit('scroll', {
    scrollTop: currentScrollTop,
    scrollHeight,
    clientHeight
  })
}

// 滚动到指定位置
function scrollToIndex(index) {
  if (containerRef.value) {
    const top = index * props.itemHeight
    containerRef.value.scrollTo({
      top,
      behavior: 'smooth'
    })
  }
}

// 滚动到顶部
function scrollToTop() {
  if (containerRef.value) {
    containerRef.value.scrollTo({
      top: 0,
      behavior: 'smooth'
    })
  }
}

// 监听 items 变化，重置滚动位置
watch(() => props.items.length, (newLength, oldLength) => {
  if (newLength < oldLength) {
    // items 减少时，滚动到顶部
    scrollTop.value = 0
    if (containerRef.value) {
      containerRef.value.scrollTop = 0
    }
  }
})

// 暴露方法
defineExpose({
  scrollToIndex,
  scrollToTop,
  getScrollTop: () => scrollTop.value
})
</script>

<style scoped>
.virtual-list {
  position: relative;
  will-change: transform;
}

.virtual-list-spacer {
  will-change: transform;
}

.virtual-list-item {
  will-change: transform;
}

.virtual-list-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.loading-spinner {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-tertiary);
  font-size: 14px;
}

.virtual-list-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 200px;
}

.empty-state {
  text-align: center;
  color: var(--color-tertiary);
}

.empty-state svg {
  font-size: 48px;
  margin-bottom: 12px;
}

.empty-state p {
  margin: 0;
  font-size: 14px;
}

/* 减少动画支持 */
@media (prefers-reduced-motion: reduce) {
  .virtual-list,
  .virtual-list-spacer,
  .virtual-list-item {
    will-change: auto;
  }
}
</style>
