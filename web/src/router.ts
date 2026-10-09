/**
 * 模块: web/src/router.ts
 * 职责: 前端路由——导航项与页面组件映射，未登录跳转登录页
 */

import { createRouter, createWebHistory } from 'vue-router'
import { api } from './api/client'

import DashboardView from './views/DashboardView.vue'
import TasksView from './views/TasksView.vue'
import SchedulesView from './views/SchedulesView.vue'
import NewsView from './views/NewsView.vue'
import DeliveriesView from './views/DeliveriesView.vue'
import SettingsView from './views/SettingsView.vue'
import LoginView from './views/LoginView.vue'

/** 导航表：侧边与顶栏都按它渲染，新增页面只改这里。 */
export const NAV = [
  { name: 'dashboard', path: '/', label: '总览' },
  { name: 'tasks', path: '/tasks', label: '任务' },
  { name: 'schedules', path: '/schedules', label: '定时推送' },
  { name: 'news', path: '/news', label: '资讯库' },
  { name: 'deliveries', path: '/deliveries', label: '推送记录' },
  { name: 'settings', path: '/settings', label: '设置' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    { path: '/', name: 'dashboard', component: DashboardView },
    { path: '/tasks', name: 'tasks', component: TasksView },
    { path: '/schedules', name: 'schedules', component: SchedulesView },
    { path: '/news', name: 'news', component: NewsView },
    { path: '/deliveries', name: 'deliveries', component: DeliveriesView },
    { path: '/settings', name: 'settings', component: SettingsView },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

/** 进入非公开页面前确认登录态，过期就回登录页。 */
router.beforeEach(async (to) => {
  if (to.meta.public) return true
  try {
    await api.me()
    return true
  } catch {
    return { name: 'login', query: { next: to.fullPath } }
  }
})
