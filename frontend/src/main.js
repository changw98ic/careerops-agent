import { createApp } from 'vue'
import 'ant-design-vue/dist/reset.css'

import App from './App.vue'
import router from './router.js'
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

void mountApplication()
