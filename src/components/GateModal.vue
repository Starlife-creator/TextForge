<script setup lang="ts">
/**
 * 人工闸门弹窗的展示层外壳（现代东方书卷工坊视觉体系）。
 *
 * 拆书确认、断层勾选、逐章验收、质检拦截、蓝图确认这五个弹窗共用同一套
 * 「毛玻璃遮罩 + 宣纸/墨砚卷匣 + 朱砂印标题栏 + 底部操作条」结构。
 *
 * 注意遮罩点击关闭**默认关闭**：闸门弹窗要求显式选择，确认/拒绝不能靠误点遮罩。
 */
import { onMounted, ref } from 'vue';

withDefaults(
  defineProps<{
    title: string;
    wide?: boolean;
    maskClose?: boolean;
  }>(),
  { wide: false, maskClose: false },
);

const emit = defineEmits<{ (e: 'close'): void }>();
const root = ref<HTMLElement | null>(null);
// 打开即聚焦容器：键盘/读屏用户不至于落在遮罩后面的页面上。
// 刻意**不**响应 Escape：闸门要求显式选择，不存在「按 Esc 不了了之」。
onMounted(() => root.value?.focus());
</script>

<template>
  <div class="modal-mask" @click.self="maskClose && emit('close')">
    <div
      ref="root"
      class="modal gate-modal"
      :class="{ 'modal-wide': wide }"
      role="dialog"
      aria-modal="true"
      :aria-label="title"
      tabindex="-1"
    >
      <div class="gate-head">
        <span class="gate-seal" aria-hidden="true">审</span>
        <h3>{{ title }}</h3>
      </div>
      <div class="gate-body">
        <slot />
      </div>
      <div v-if="$slots.actions" class="modal-actions">
        <slot name="actions" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.gate-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-bottom: 12px;
  margin-bottom: 12px;
  border-bottom: 1px solid var(--tf-border);
}

.gate-head h3 {
  margin: 0;
  font-family: var(--tf-font-serif);
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.gate-seal {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: var(--tf-radius-xs);
  background: var(--tf-accent-soft);
  border: 1px solid var(--tf-accent-border);
  color: var(--tf-accent);
  font-family: var(--tf-font-serif);
  font-size: 12px;
  font-weight: 700;
  flex-shrink: 0;
}

.gate-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
</style>
