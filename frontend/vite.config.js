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
  },
})
