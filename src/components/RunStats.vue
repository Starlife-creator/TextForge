<script setup lang="ts">
/**
 * 运行统计与进度条（纯展示，无任何副作用）。
 *
 * 只接收父组件已经算好的数值与百分比，使用等宽数字（tabular-nums）稳定排版，
 * 避免流式 token 与读秒跳动时产生水平抖动。
 */
defineProps<{
  totalChapters: number;
  batchStats: { phase1: number; phase3: number; total: number };
  chapterProgress: { done: number; total: number };
  usageTokens: { prompt: number | string; completion: number | string };
  /** 按用户填的单价折算的已花费；未填单价时为「本地模型（未计单价）」 */
  spentText?: string;
  elapsed: string;
  pipelineRunning: boolean;
  /** 三个百分比由父组件算好（字符串，含 % 号），这里不再做业务计算 */
  diagPercent: string;
  reconPercent: string;
  chapterPercent: string;
}>();

function fmtNum(v: number | string): string {
  if (typeof v === 'number' && Number.isFinite(v)) return v.toLocaleString('zh-CN');
  return String(v);
}
</script>

<template>
  <div class="stats">
    <span class="stat">
      总章节：<b>{{ totalChapters || '-' }}</b>
    </span>
    <span class="stat">
      诊断批次：<b>{{ batchStats.phase1 }}/{{ batchStats.total }}</b>
    </span>
    <span class="stat">
      重构批次：<b>{{ batchStats.phase3 }}/{{ batchStats.total }}</b>
    </span>
    <span v-if="pipelineRunning || elapsed !== '00:00'" class="stat">
      耗时：<b>{{ elapsed }}</b>
    </span>
    <span class="stat">
      Tokens：<b>{{ fmtNum(usageTokens.prompt) }}</b> 入 /
      <b>{{ fmtNum(usageTokens.completion) }}</b> 出
    </span>
    <span v-if="spentText" class="stat">
      花费：<b>{{ spentText }}</b>
    </span>
  </div>

  <div class="bars">
    <div class="bar">
      <span class="bar-label">诊断批次</span>
      <div class="bar-track"><div class="bar-fill" :style="{ width: diagPercent }"></div></div>
      <span class="bar-num"
        >{{ batchStats.phase1 }}/{{ batchStats.total }} · {{ diagPercent }}</span
      >
    </div>
    <div class="bar">
      <span class="bar-label">重构批次</span>
      <div class="bar-track">
        <div class="bar-fill recon" :style="{ width: reconPercent }"></div>
      </div>
      <span class="bar-num"
        >{{ batchStats.phase3 }}/{{ batchStats.total }} · {{ reconPercent }}</span
      >
    </div>
    <div v-if="chapterProgress.total" class="bar">
      <span class="bar-label">已重构章</span>
      <div class="bar-track">
        <div class="bar-fill chapter" :style="{ width: chapterPercent }"></div>
      </div>
      <span class="bar-num">
        {{ chapterProgress.done }}/{{ chapterProgress.total }} · {{ chapterPercent }}
      </span>
    </div>
  </div>
</template>
