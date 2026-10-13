import { createApp } from "vue";
import App from "./App.vue";
// 弹窗/列表的共用样式必须全局引入：App.vue 的 <style scoped> 不会作用到
// 子组件内部元素，拆出的历史、版本等弹窗会因此完全丢失样式。
import "./styles/shared.css";

createApp(App).mount("#app");
