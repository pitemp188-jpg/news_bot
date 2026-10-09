// 模块: web/eslint.config.js
// 职责: 前端 lint 规则——Vue 3 推荐集 + TS 解析 + 禁止页面直接发 HTTP（只允许 api/ 目录）
import tsParser from '@typescript-eslint/parser'
import pluginVue from 'eslint-plugin-vue'

export default [
  { files: ['**/*.{js,ts,vue}'] },
  // 纯 TS 文件也用 TS 解析器
  {
    files: ['**/*.ts'],
    languageOptions: { parser: tsParser, ecmaVersion: 2022, sourceType: 'module' },
  },
  ...pluginVue.configs['flat/recommended'],
  {
    // .vue 里的 <script lang="ts"> 交给 TS 解析器，否则会报解析错误
    languageOptions: {
      parserOptions: {
        parser: tsParser,
        ecmaVersion: 2022,
        sourceType: 'module',
      },
    },
    rules: {
      'vue/multi-word-component-names': 'off',
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
      'vue/html-indent': 'off',
      'vue/attributes-order': 'off',
      'vue/first-attribute-linebreak': 'off',
      'vue/html-closing-bracket-newline': 'off',
    },
  },
  {
    // 只有 api/ 目录可以直接发请求，其余页面一律走封装
    files: ['src/**/*.{ts,vue}'],
    ignores: ['src/api/**'],
    rules: {
      'no-restricted-globals': [
        'error',
        { name: 'fetch', message: '请通过 src/api/client.ts 发请求，不要在页面里直接 fetch' },
      ],
    },
  },
]
