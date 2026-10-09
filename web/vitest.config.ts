// 模块: web/vitest.config.ts
// 职责: 单测配置——happy-dom 环境，只跑 src 下的 *.spec.ts
// 说明: 与 vite.config.ts 分开，因为 vitest 自带的 vite 版本与项目 vite 6 类型不同。
import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'happy-dom',
    globals: true,
    include: ['src/**/*.spec.ts'],
  },
})
