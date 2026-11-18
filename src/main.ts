import { createApp } from 'vue';
import App from './App.vue';
import { applyThemeToRoot, resolveInitialTheme } from './theme';
// 弹窗/列表与工坊设计令牌全局引入：App.vue 的 <style scoped> 不会作用到子组件内部元素。
import './styles/shared.css';

// 首帧挂载前同步套用「宣纸 / 墨砚」主题，避免深色用户启动闪白，同时严守 Tauri CSP（无内联脚本）。
applyThemeToRoot(resolveInitialTheme(typeof localStorage !== 'undefined' ? localStorage : null));

const app = createApp(App);

// 兜底：组件内未捕获的异常与未处理的 Promise 拒绝此前会静默消失（界面空白、
// 点了没反应却查不到原因）。统一打到 console，便于排查。
app.config.errorHandler = (err, _instance, info) => {
  console.error('[TextForge] 未捕获的组件错误:', err, info);
};
window.addEventListener('unhandledrejection', (e) => {
  console.error('[TextForge] 未处理的 Promise 拒绝:', e.reason);
});

app.mount('#app');
