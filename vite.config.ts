import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import process from 'node:process';
import { readFileSync } from 'node:fs';
const host = process.env.TAURI_DEV_HOST;

// 版本号单一来源：package.json。注入 __APP_VERSION__ 供界面显示（见 App.vue）；
// 后端版本由外壳注入的 TEXTFORGE_VERSION 提供 —— 同源于 package.json。
const appVersion: string = JSON.parse(
  readFileSync(new URL('./package.json', import.meta.url), 'utf-8'),
).version;

// https://vite.dev/config/
export default defineConfig(() => ({
  plugins: [vue()],
  define: { __APP_VERSION__: JSON.stringify(appVersion) },

  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,
  // 2. tauri expects a fixed port, fail if that port is not available
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: 'ws',
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      // 3. tell Vite to ignore watching `src-tauri`
      ignored: ['**/src-tauri/**'],
    },
  },
}));
