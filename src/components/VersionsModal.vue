<script setup lang="ts">
/**
 * 成品版本弹窗（重跑前的整版快照，可回滚）。
 *
 * 与 HistoryModal 同样是从 App.vue 拆出。只负责展示；恢复动作会改磁盘，
 * 交由 App.vue 执行（它掌握 outputPath / 提示框 / 状态刷新）。
 */
import { onMounted, onUnmounted, ref } from 'vue';
import type { VersionInfo } from '../api/system';

defineProps<{
  versions: VersionInfo[];
  busy: boolean;
}>();

const emit = defineEmits<{
  (e: 'restore', v: VersionInfo): void;
  (e: 'close'): void;
}>();

// 可关闭弹窗：Escape 关闭 + 打开即聚焦（键盘/读屏可用性）。
const root = ref<HTMLElement | null>(null);
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close');
}
onMounted(() => {
  window.addEventListener('keydown', onKey);
  root.value?.focus();
});
onUnmounted(() => window.removeEventListener('keydown', onKey));

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div
      ref="root"
      class="modal"
      role="dialog"
      aria-modal="true"
      aria-label="成品版本"
      tabindex="-1"
    >
      <h3>成品版本</h3>
      <p class="hint">
        每次重跑覆盖成品前，都会把上一版完整快照到这里（保留最近 10
        版）。点「恢复」即可回滚；恢复前当前版本也会再留一份。
      </p>
      <div v-if="busy" class="log-empty">读取中…</div>
      <div v-else-if="!versions.length" class="log-empty">
        还没有历史版本（重跑一次就会自动留存上一版）
      </div>
      <div class="sum-list">
        <div v-for="v in versions" :key="v.id" class="split-row hist-row">
          <div class="hist-main">
            <div class="hist-title">{{ v.id }}</div>
            <div class="hint">{{ v.files }} 个文件 · {{ fmtSize(v.size) }}</div>
            <div class="hint mono">
              {{ v.names.slice(0, 3).join('、') }}{{ v.names.length > 3 ? '…' : '' }}
            </div>
          </div>
          <div class="hist-actions">
            <button class="open-btn" :disabled="busy" @click="emit('restore', v)">恢复</button>
          </div>
        </div>
      </div>
      <div class="modal-actions">
        <span></span>
        <button type="button" @click="emit('close')">关闭</button>
      </div>
    </div>
  </div>
</template>
