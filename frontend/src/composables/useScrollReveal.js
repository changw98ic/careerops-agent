import { ref, onMounted, onUnmounted } from 'vue'

/**
 * 滚动揭示动画 composable
 * 使用 IntersectionObserver 实现元素进入视口时的动画效果
 * 支持 prefers-reduced-motion 无障碍访问
 */
export function useScrollReveal() {
  const prefersReducedMotion = ref(false)
  let motionQuery = null
  let revealObserver = null

  /**
   * 初始化动画系统
   */
  function setupRevealObserver() {
    // 检查用户是否偏好减少动画
    motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
    prefersReducedMotion.value = motionQuery.matches

    // 监听变化
    motionQuery.addEventListener('change', (e) => {
      prefersReducedMotion.value = e.matches
      if (e.matches) {
        // 如果用户偏好减少动画，立即显示所有元素
        document.querySelectorAll('[data-reveal]').forEach((el) => {
          el.classList.add('is-revealed')
        })
      }
    })

    // 如果用户偏好减少动画，不创建观察者
    if (prefersReducedMotion.value) {
      document.querySelectorAll('[data-reveal]').forEach((el) => {
        el.classList.add('is-revealed')
      })
      return
    }

    // 创建 IntersectionObserver
    revealObserver = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const el = entry.target

            // 应用交错延迟
            const stagger = el.dataset.stagger
            if (stagger !== undefined) {
              const delay = Number(stagger) * 80
              el.style.transitionDelay = `${delay}ms`
            }

            // 添加揭示类
            el.classList.add('is-revealed')

            // 停止观察
            revealObserver.unobserve(el)
          }
        })
      },
      {
        threshold: 0.1,
        rootMargin: '0px 0px -40px 0px'
      }
    )

    // 观察所有带有 data-reveal 属性的元素
    document.querySelectorAll('[data-reveal]').forEach((el) => {
      revealObserver.observe(el)
    })
  }

  /**
   * 观察新添加的元素
   * 用于动态加载的内容
   */
  function observeNewReveals() {
    if (!revealObserver || prefersReducedMotion.value) return

    document.querySelectorAll('[data-reveal]:not(.is-revealed)').forEach((el) => {
      revealObserver.observe(el)
    })
  }

  /**
   * 清理观察者
   */
  function cleanup() {
    if (revealObserver) {
      revealObserver.disconnect()
      revealObserver = null
    }
    if (motionQuery) {
      motionQuery.removeEventListener('change')
      motionQuery = null
    }
  }

  // 生命周期钩子
  onMounted(() => {
    setupRevealObserver()
  })

  onUnmounted(() => {
    cleanup()
  })

  return {
    prefersReducedMotion,
    observeNewReveals,
    cleanup
  }
}
