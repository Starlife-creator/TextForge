<script setup lang="ts">
/**
 * 任务历史弹窗（跨会话）。
 *
 * 从 App.vue 拆出：那里原本有 2000+ 行，所有功能挤在一个组件里，改任何一处
 * 都要在整份文件里定位，也无法为单个功能单独测试。
 *
 * 职责边界：本组件只负责**展示与筛选**；会改动主界面配置的「载入」以及
 * 会调后端的「移除 / 清空」一律 emit 给 App.vue 处理，避免状态双向纠缠。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';
import type { HistoryItem } from '../api/system';

const props = defineProps<{
  items: HistoryItem[];
  query: string;
  sort: 'recent' | 'name' | 'progress';
  busy: boolean;
}>();

const emit = defineEmits<{
  (e: 'update:query', v: string): void;
  (e: 'update:sort', v: 'recent' | 'name' | 'progress'): void;
  (e: 'refresh'): void;
  (e: 'load', h: HistoryItem): void;
  (e: 'forget', h: HistoryItem): void;
  (e: 'clear'): void;
  (e: 'close'): void;
}>();

/** 过滤 + 排序后的可见列表。搜索同时匹配书名与输入/输出路径。 */
const view = computed(() => {
  const q = props.query.trim().toLowerCase();
  let list = props.items.filter((h) => {
    if (!q) return true;
    return [h.novel_name, h.output_path, h.input_path].some((v) =>
      (v || '').toLowerCase().includes(q),
    );
  });
  const byUpdated = (a: HistoryItem, b: HistoryItem) =>
    (b.updated_at || '').localeCompare(a.updated_at || '');
  if (props.sort === 'recent') list = [...list].sort(byUpdated);
  else if (props.sort === 'name') {
    list = [...list].sort((a, b) =>
      (a.novel_name || a.output_path || '').localeCompare(b.novel_name || b.output_path || ''),
    );
  } else {
    // 进度低的排前面：它们才是用户回来要续跑的
    list = [...list].sort((a, b) => {
      const pa = a.batches_total ? (a.batches_done || 0) / a.batches_total : a.finished ? 1 : 0;
      const pb = b.batches_total ? (b.batches_done || 0) / b.batches_total : b.finished ? 1 : 0;
      return pa - pb || byUpdated(a, b);
    });
  }
  return list;
});

function state(h: HistoryItem): { text: string; cls: string } {
  if (h.running) return { text: '运行中', cls: 'badge running' };
  if (h.finished) return { text: '已完成', cls: 'badge ok' };
  if (h.last_error) return { text: '出错中断', cls: 'badge err2' };
  if (h.current_phase && h.current_phase !== 'phase0')
    return { text: '未完成·可续跑', cls: 'badge paused' };
  return { text: '未开始', cls: '' };
}

/** 批次进度百分比（无批次信息时按完成与否给 0/100） */
function pct(h: HistoryItem): number {
  if (h.finished) return 100;
  if (!h.batches_total) return 0;
  return Math.min(100, Math.round(((h.batches_done || 0) / h.batches_total) * 100));
}

/** 相对时间：比 ISO 串友好得多（"3 分钟前" vs "2026-10-12T04:56:33Z"） */
function ago(iso?: string | null): string {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return '刚刚';
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`;
  return iso.slice(0, 10);
}

// 可关闭弹窗：Escape 关闭 + 打开即聚焦，键盘用户不必逐项 Tab 穿过整页。
const root = ref<HTMLElement | null>(null);
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close');
}
onMounted(() => {
  window.addEventListener('keydown', onKey);
  root.value?.focus();
});
onUnmounted(() => window.removeEventListener('keydown', onKey));
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div
      ref="root"
      class="modal"
      role="dialog"
      aria-modal="true"
      aria-label="任务历史"
      tabindex="-1"
    >
      <h3>任务历史</h3>
      <p class="hint">
        记录你跑过的每一本书：跑到哪一阶段、上次为什么停的都在这里。点「载入」填回配置，再点开始即可断点续跑。
      </p>
      <div class="hist-toolbar">
        <input
          :value="query"
          type="search"
          aria-label="搜索任务"
          placeholder="搜索书名或路径…"
          @input="emit('update:query', ($event.target as HTMLInputElement).value)"
        />
        <select
          :value="sort"
          aria-label="排序方式"
          @change="emit('update:sort', ($event.target as HTMLSelectElement).value as any)"
        >
          <option value="recent">最近更新</option>
          <option value="progress">进度（未完成优先）</option>
          <option value="name">书名</option>
        </select>
        <button class="ghost" :disabled="busy" @click="emit('refresh')">
          {{ busy ? '刷新中…' : '刷新' }}
        </button>
      </div>
      <div v-if="!view.length" class="log-empty">
        {{ items.length ? '没有匹配的任务' : '还没有历史任务' }}
      </div>
      <div class="sum-list">
        <div v-for="h in view" :key="h.output_path" class="split-row hist-row">
          <div class="hist-main">
            <div class="hist-title">
              {{ h.novel_name || '（未命名）' }}
              <span class="badge" :class="state(h).cls">{{ state(h).text }}</span>
              <span v-if="h.is_current" class="badge paused">当前目录</span>
              <span class="hist-ago">{{ ago(h.updated_at) }}</span>
            </div>
            <div class="hist-bar" :title="`批次 ${h.batches_done || 0}/${h.batches_total || 0}`">
              <div
                class="hist-bar-fill"
                :style="{ width: pct(h) + '%' }"
                :class="{ done: h.finished, bad: !!h.last_error }"
              ></div>
            </div>
            <div class="hint">
              {{ h.total_chapters || 0 }} 章 · 批次 {{ h.batches_done || 0 }}/{{
                h.batches_total || 0
              }}
              <template v-if="h.current_phase">· {{ h.current_phase }}</template>
            </div>
            <div class="hint mono">{{ h.output_path }}</div>
            <div v-if="h.last_error" class="out-status err">上次中断：{{ h.last_error }}</div>
            <div v-if="h.config_drift && h.config_drift.length" class="out-status err">
              续跑配置与上次不同：{{ h.config_drift.join('；') }}
            </div>
          </div>
          <div class="hist-actions">
            <button class="open-btn" @click="emit('load', h)">载入</button>
            <button class="open-btn ghost" @click="emit('forget', h)">移除</button>
          </div>
        </div>
      </div>
      <div class="modal-actions">
        <button v-if="items.length" class="ghost" @click="emit('clear')">清空历史记录</button>
        <span></span>
        <button type="button" @click="emit('close')">关闭</button>
      </div>
    </div>
  </div>
</template>
