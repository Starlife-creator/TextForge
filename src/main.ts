import { createApp } from 'vue';
import App from './App.vue';
// 弹窗/列表的共用样式必须全局引入：App.vue 的 <style scoped> 不会作用到
// 子组件内部元素，拆出的历史、版本等弹窗会因此完全丢失样式。
import './styles/shared.css';

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
