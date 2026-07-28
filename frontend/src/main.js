import { createApp } from 'vue'
import 'ant-design-vue/dist/reset.css'

import App from './App.vue'
import router from './router.js'
import './fonts.css'
import './styles.css'

async function mountApplication() {
  const app = createApp(App)
  app.use(router)

  // Keep the HTML entry small. The component registry is needed before the
  // first route renders, but it does not belong in the browser bootstrap
  // chunk shared by every page.
  const { registerUiComponents } = await import('./ui-components.js')
  registerUiComponents(app)
  await router.isReady()
  app.mount('#app')
}

void mountApplication().catch((err) => {
  // Keep a failed lazy registry/import from producing a blank page. Details
  // stay in the console; the visible message is intentionally non-sensitive.
  console.error('CareerOps failed to mount', err)
  const root = document.querySelector('#app')
  if (root) {
    root.innerHTML = '<main class="bootstrap-error"><h1>应用加载失败</h1><p>请刷新页面重试。</p></main>'
  }
})
