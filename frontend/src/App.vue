<template>
  <nav v-if="isLoggedIn" class="nav">
    <router-link to="/jobs">Jobs</router-link>
    <router-link to="/companies">Companies</router-link>
    <router-link to="/applications">Applications</router-link>
    <a href="#" @click.prevent="logout" class="logout">Logout</a>
  </nav>
  <main class="container">
    <router-view />
  </main>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const isLoggedIn = computed(() => document.cookie.includes('careerops_session='))

function logout() {
  document.cookie = 'careerops_session=; Max-Age=0; Path=/'
  document.cookie = 'careerops_csrf=; Max-Age=0; Path=/'
  router.push('/login')
}
</script>

<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }
.container { max-width: 1200px; margin: 0 auto; padding: 24px; }
.nav { display: flex; gap: 16px; padding: 12px 24px; background: #1a1a2e; align-items: center; }
.nav a { color: #e0e0e0; text-decoration: none; font-size: 14px; padding: 6px 12px; border-radius: 4px; }
.nav a:hover, .nav a.router-link-active { background: #16213e; color: #fff; }
.nav .logout { margin-left: auto; color: #ff6b6b; }
table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #eee; }
th { background: #f8f9fa; font-weight: 600; font-size: 13px; color: #666; text-transform: uppercase; }
tr:hover { background: #f8f9fa; }
button { padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
.btn-primary { background: #4361ee; color: #fff; }
.btn-primary:hover { background: #3a56d4; }
.btn-success { background: #2ec4b6; color: #fff; }
.btn-success:hover { background: #28b0a3; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
input, select { padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
input:focus, select:focus { outline: none; border-color: #4361ee; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 500; }
.badge-active { background: #d4edda; color: #155724; }
.badge-submitted { background: #cce5ff; color: #004085; }
.badge-favorited { background: #fff3cd; color: #856404; }
h1 { margin-bottom: 20px; font-size: 24px; }
.toolbar { display: flex; gap: 12px; margin-bottom: 16px; align-items: center; flex-wrap: wrap; }
</style>
