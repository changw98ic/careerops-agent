import { ref, shallowRef } from 'vue'

/**
 * 图表懒加载 composable
 * 使用动态导入实现图表库的按需加载
 */
export function useChart() {
  const Chart = ref(null)
  const loading = ref(false)
  const error = ref(null)

  /**
   * 加载 G2 图表库
   */
  async function loadG2() {
    if (Chart.value) return Chart.value

    loading.value = true
    error.value = null

    try {
      const module = await import('@antv/g2')
      Chart.value = module.Chart
      return Chart.value
    } catch (err) {
      error.value = err
      console.error('[useChart] Failed to load G2:', err)
      throw err
    } finally {
      loading.value = false
    }
  }

  /**
   * 创建图表实例
   */
  async function createChart(container, options = {}) {
    const ChartClass = await loadG2()
    return new ChartClass({
      container,
      autoFit: true,
      height: 280,
      ...options,
    })
  }

  return {
    Chart,
    loading,
    error,
    loadG2,
    createChart,
  }
}
