import pluginVue from 'eslint-plugin-vue';
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript';
import skipFormatting from '@vue/eslint-config-prettier/skip-formatting';

// 扁平配置（ESLint 10）。skipFormatting 必须放最后：它关掉所有与 Prettier
// 重叠的格式类规则，让「ESLint 管代码质量、Prettier 管排版」各司其职。
export default defineConfigWithVueTs(
  {
    name: 'app/files-to-lint',
    files: ['**/*.{ts,mts,tsx,vue}'],
  },
  {
    name: 'app/files-to-ignore',
    ignores: [
      '**/dist/**',
      '**/dist-ssr/**',
      '**/coverage/**',
      '**/node_modules/**',
      '**/src-tauri/**',
    ],
  },
  pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,
  {
    name: 'app/rules',
    rules: {
      // 后端返回的是动态 JSON，接口层大量使用 any 是有意为之；类型安全由
      // `vue-tsc --noEmit`（npm run build 里）把关，这里不重复报错。
      '@typescript-eslint/no-explicit-any': 'off',
      // 允许以下划线开头的变量/参数（如 `_showSettings`）被标记为故意未使用
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    },
  },
  {
    name: 'app/declarations',
    files: ['**/*.d.ts'],
    rules: {
      // *.vue 的模块声明模板，空对象类型是 Vue 官方写法。
      '@typescript-eslint/no-empty-object-type': 'off',
    },
  },
  skipFormatting,
);
