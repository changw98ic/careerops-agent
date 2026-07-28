/**
 * 性能监控工具
 * 用于监控页面加载性能和运行时性能
 */

// 性能指标收集
const metrics = {
  // 页面加载指标
  load: {},
  // 运行时指标
  runtime: {},
  // 资源加载指标
  resources: [],
}

/**
 * 收集页面加载性能指标
 */
export function collectLoadMetrics() {
  if (!window.performance) return

  const navigation = performance.getEntriesByType('navigation')[0]
  if (!navigation) return

  metrics.load = {
    // DNS 查询时间
    dnsLookup: navigation.domainLookupEnd - navigation.domainLookupStart,
    // TCP 连接时间
    tcpConnect: navigation.connectEnd - navigation.connectStart,
    // 请求响应时间
    requestResponse: navigation.responseEnd - navigation.requestStart,
    // DOM 解析时间
    domParse: navigation.domInteractive - navigation.responseEnd,
    // DOM 完成时间
    domComplete: navigation.domComplete - navigation.domInteractive,
    // 页面加载总时间
    loadComplete: navigation.loadEventEnd - navigation.startTime,
    // 首次内容绘制
    firstPaint: getFirstPaint(),
    // 最大内容绘制
    largestContentPaint: getLargestContentPaint(),
    // 累积布局偏移
    cumulativeLayoutShift: getCumulativeLayoutShift(),
    // 首次输入延迟
    firstInputDelay: getFirstInputDelay(),
  }

  return metrics.load
}

/**
 * 获取首次内容绘制时间
 */
function getFirstPaint() {
  const paintEntries = performance.getEntriesByType('paint')
  const fp = paintEntries.find((entry) => entry.name === 'first-paint')
  return fp ? fp.startTime : null
}

/**
 * 获取最大内容绘制时间
 */
function getLargestContentPaint() {
  return new Promise((resolve) => {
    new PerformanceObserver((list) => {
      const entries = list.getEntries()
      const lastEntry = entries[entries.length - 1]
      resolve(lastEntry.startTime)
    }).observe({ type: 'largest-contentful-paint', buffered: true })
  })
}

/**
 * 获取累积布局偏移
 */
function getCumulativeLayoutShift() {
  return new Promise((resolve) => {
    let clsValue = 0
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (!entry.hadRecentInput) {
          clsValue += entry.value
        }
      }
      resolve(clsValue)
    }).observe({ type: 'layout-shift', buffered: true })
  })
}

/**
 * 获取首次输入延迟
 */
function getFirstInputDelay() {
  return new Promise((resolve) => {
    new PerformanceObserver((list) => {
      const firstInput = list.getEntries()[0]
      resolve(firstInput.processingStart - firstInput.startTime)
    }).observe({ type: 'first-input', buffered: true })
  })
}

/**
 * 收集资源加载性能指标
 */
export function collectResourceMetrics() {
  if (!window.performance) return

  const resources = performance.getEntriesByType('resource')
  metrics.resources = resources.map((resource) => ({
    name: resource.name,
    type: resource.initiatorType,
    duration: resource.duration,
    size: resource.transferSize,
    startTime: resource.startTime,
  }))

  return metrics.resources
}

/**
 * 监控运行时性能
 */
export function monitorRuntimePerformance() {
  // 监控长任务
  if ('PerformanceObserver' in window) {
    const longTaskObserver = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        console.warn('[Performance] Long task detected:', {
          duration: entry.duration,
          startTime: entry.startTime,
          name: entry.name,
        })
      }
    })
    longTaskObserver.observe({ entryTypes: ['longtask'] })
  }

  // 监控内存使用
  if (performance.memory) {
    setInterval(() => {
      const memory = performance.memory
      metrics.runtime.memory = {
        usedJSHeapSize: memory.usedJSHeapSize,
        totalJSHeapSize: memory.totalJSHeapSize,
        jsHeapSizeLimit: memory.jsHeapSizeLimit,
      }

      // 内存使用超过 80% 时警告
      if (memory.usedJSHeapSize / memory.jsHeapSizeLimit > 0.8) {
        console.warn('[Performance] High memory usage:', {
          used: formatBytes(memory.usedJSHeapSize),
          limit: formatBytes(memory.jsHeapSizeLimit),
        })
      }
    }, 30000) // 每 30 秒检查一次
  }
}

/**
 * 格式化字节大小
 */
function formatBytes(bytes) {
  if (bytes === 0) return '0 Bytes'
  const k = 1024
  const sizes = ['Bytes', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

/**
 * 获取性能报告
 */
export function getPerformanceReport() {
  return {
    load: metrics.load,
    runtime: metrics.runtime,
    resources: metrics.resources,
    timestamp: Date.now(),
  }
}

/**
 * 初始化性能监控
 */
export function initPerformanceMonitoring() {
  // 收集加载指标
  if (document.readyState === 'complete') {
    collectLoadMetrics()
  } else {
    window.addEventListener('load', () => {
      setTimeout(collectLoadMetrics, 0)
    })
  }

  // 收集资源指标
  collectResourceMetrics()

  // 监控运行时性能
  monitorRuntimePerformance()

  // 定期收集资源指标
  setInterval(collectResourceMetrics, 60000) // 每分钟收集一次

  console.log('[Performance] Monitoring initialized')
}
