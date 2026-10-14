<script setup lang="ts">
/**
 * 运行统计与进度条（纯展示，无任何副作用）。
 *
 * 从 App.vue 抽出：这里原本是一大块内联模板，所有数据都从上百个 ref 里取，
 * 与主界面的状态逻辑缠在一起。拆出来后它只接收**已经算好的值**，
 * 既能单独理解，也能单独写渲染测试。
 */
defineProps<{
  totalChapters: number;
  batchStats: { phase1: number; phase3: number; total: number };
  chapterProgress: { done: number; total: number };
  usageTokens: { prompt: number | string; completion: number | string };
  elapsed: string;
  pipelineRunning: boolean;
  /** 三个百分比由父组件算好（字符串，含 % 号），这里不再做业务计算 */
  diagPercent: string;
  reconPercent: string;
  chapterPercent: string;
}>();
</script>

<template>
  <div class="stats">
    <span class="stat">总章节：<b>{{ totalChapters || '-' }}</b></span>
    <span class="stat">批次：<b>{{ batchStats.phase1 }}/{{ batchStats.total }}</b>（诊断）</span>
    <span class="stat">重构：<b>{{ batchStats.phase3 }}/{{ batchStats.total }}</b></span>
    <span class="stat" v-if="pipelineRunning || elapsed !== '00:00'">已运行：<b>{{ elapsed }}</b></span>
    <span class="stat">Tokens：<b>{{ usageTokens.prompt }}</b>输入 / <b>{{ usageTokens.completion }}</b>输出</span>
  </div>

  <div class="bars">
    <div class="bar">
      <span class="bar-label">诊断批次</span>
      <div class="bar-track"><div class="bar-fill" :style="{ width: diagPercent }"></div></div>
      <span class="bar-num">{{ batchStats.phase1 }}/{{ batchStats.total }} · {{ diagPercent }}</span>
    </div>
    <div class="bar">
      <span class="bar-label">重构批次</span>
      <div class="bar-track"><div class="bar-fill recon" :style="{ width: reconPercent }"></div></div>
      <span class="bar-num">{{ batchStats.phase3 }}/{{ batchStats.total }} · {{ reconPercent }}</span>
    </div>
    <div v-if="chapterProgress.total" class="bar">
      <span class="bar-label">已重构章</span>
      <div class="bar-track"><div class="bar-fill chapter" :style="{ width: chapterPercent }"></div></div>
      <span class="bar-num">{{ chapterProgress.done }}/{{ chapterProgress.total }} · {{ chapterPercent }}</span>
    </div>
  </div>
</template>