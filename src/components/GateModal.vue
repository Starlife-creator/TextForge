<script setup lang="ts">
/**
 * 人工闸门弹窗的展示层外壳。
 *
 * 拆书确认、断层勾选、逐章验收、质检拦截、蓝图确认这五个弹窗共用同一套
 * 「遮罩 + 容器 + 标题 + 底部按钮条」结构，之前各写一遍。抽出来后：
 *   - 外壳样式只有这一处定义，改主题不会漏改；
 *   - 各弹窗只关心自己的内容与按钮。
 *
 * 注意遮罩点击关闭**默认关闭**：闸门弹窗要求显式选择，确认/拒绝不能靠误点遮罩。
 * 需要时可开 maskClose。
 */
withDefaults(defineProps<{
  title: string;
  wide?: boolean;
  maskClose?: boolean;
}>(), { wide: false, maskClose: false });

const emit = defineEmits<{ (e: 'close'): void }>();
</script>

<template>
  <div class="modal-mask" @click.self="maskClose && emit('close')">
    <div class="modal" :class="{ 'modal-wide': wide }">
      <h3>{{ title }}</h3>
      <slot />
      <div v-if="$slots.actions" class="modal-actions">
        <slot name="actions" />
      </div>
    </div>
  </div>
</template>