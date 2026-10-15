/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_BACKEND_PORT?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Vite define 注入的应用版本（来源：package.json，见 vite.config.ts）
declare const __APP_VERSION__: string;

declare module '*.vue' {
  import type { DefineComponent } from 'vue';
  const component: DefineComponent<{}, {}, any>;
  export default component;
}
