/**
 * 模块: web/src/main.ts
 * 职责: 前端入口——挂载应用、注册路由
 */

import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import './styles.css'

createApp(App).use(router).mount('#app')
