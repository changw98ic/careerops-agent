import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/login': 'http://127.0.0.1:8000',
      '/bootstrap': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: 'dist',
    // 使用 Rolldown 进行代码分割
    rollupOptions: {
      output: {
        // 自动代码分割
        manualChunks(id) {
          // Vue 核心库
          if (id.includes('node_modules/vue/') || id.includes('node_modules/vue-router/')) {
            return 'vue-core'
          }
          // Ant Design Vue 核心
          if (id.includes('ant-design-vue/es/button') ||
              id.includes('ant-design-vue/es/card') ||
              id.includes('ant-design-vue/es/form') ||
              id.includes('ant-design-vue/es/input') ||
              id.includes('ant-design-vue/es/table') ||
              id.includes('ant-design-vue/es/layout') ||
              id.includes('ant-design-vue/es/menu')) {
            return 'antd-core'
          }
          // Ant Design Vue 其他组件
          if (id.includes('ant-design-vue/es/')) {
            return 'antd-extra'
          }
          // 图标库
          if (id.includes('@ant-design/icons-vue')) {
            return 'antd-icons'
          }
          // 图表库
          if (id.includes('@antv/g2')) {
            return 'charts'
          }
        },
      },
    },
    // 分块大小警告限制
    chunkSizeWarningLimit: 500,
    // CSS 代码分割
    cssCodeSplit: true,
    // Source map 配置
    sourcemap: false,
  },
  // 依赖优化
  optimizeDeps: {
    include: ['vue', 'vue-router', 'ant-design-vue'],
  },
})
