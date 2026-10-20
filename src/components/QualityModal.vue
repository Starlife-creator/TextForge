<script setup lang="ts">
/**
 * 成品质量报告弹窗。
 *
 * 只展示后端已经算好的事实：状态、计数、问题清单、异常章节、跨章重复。
 * 不在这里做任何质量推断，也不把「通过」说成「写得好」—— 自动检查覆盖不到文风。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';
import type { QualityChapterRow, QualityReport } from '../api/system';

const props = defineProps<{
  report: QualityReport | null;
  busy: boolean;
  error?: string | null;
}>();

const emit = defineEmits<{ (e: 'close'): void }>();

const root = ref<HTMLElement | null>(null);
const showAll = ref(false);

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close');
}
onMounted(() => {
  window.addEventListener('keydown', onKey);
  root.value?.focus();
});
onUnmounted(() => window.removeEventListener('keydown', onKey));

const statusText = computed(() => {
  if (!props.report) return '';
  return { good: '未发现结构性问题', warn: '需要留意', bad: '发现硬性问题' }[props.report.status];
});
const statusClass = computed(() => {
  if (!props.report) return 'badge';
  return props.report.status === 'good'
    ? 'badge ok'
    : props.report.status === 'warn'
      ? 'badge paused'
      : 'badge err2';
});

const flagged = computed(() =>
  (props.report?.chapters || []).filter((c) => c.status === 'bad' || c.status === 'warn'),
);
const visibleChapters = computed(() => {
  const rows = showAll.value ? props.report?.chapters || [] : flagged.value;
  return rows.slice(0, 80);
});

function fmt(n: number | null | undefined): string {
  return typeof n === 'number' ? n.toLocaleString('zh-CN') : '-';
}
function ratioText(ratio: number | null): string {
  return ratio == null ? '-' : `${(ratio * 100).toFixed(1)}%`;
}
function rowBadge(row: QualityChapterRow): string {
  return row.status === 'bad' ? 'badge err2' : row.status === 'warn' ? 'badge paused' : 'badge ok';
}
function rowStatus(row: QualityChapterRow): string {
  return row.status === 'bad'
    ? '异常'
    : row.status === 'warn'
      ? '留意'
      : row.status === 'skipped'
        ? '空章'
        : '正常';
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div
      ref="root"
      class="modal q-modal"
      role="dialog"
      aria-modal="true"
      aria-label="成品质量报告"
      tabindex="-1"
    >
      <h3>成品质量报告</h3>

      <div v-if="busy" class="log-empty">读取中…</div>
      <div v-else-if="!report" class="log-empty">{{ error || '尚无质量报告' }}</div>

      <template v-else>
        <p class="q-headline">
          <span :class="statusClass">{{ statusText }}</span>
          <span class="q-headline-text">{{ report.headline }}</span>
          <span class="hint">{{ report.generated_at }}</span>
        </p>

        <div class="q-summary">
          <div class="q-stat">
            <span class="q-k">章节</span>
            <b>{{ fmt(report.summary.chapters_reconstructed) }}</b>
            <span class="hint"
              >/ {{ fmt(report.summary.chapters) }}（空章跳过
              {{ fmt(report.summary.chapters_skipped_empty) }}）</span
            >
          </div>
          <div class="q-stat">
            <span class="q-k">总字数</span>
            <b>{{ fmt(report.summary.total_reconstructed_chars) }}</b>
            <span class="hint">
              原文 {{ fmt(report.summary.total_original_chars) }} · 比例
              {{ ratioText(report.summary.length_ratio) }}
            </span>
          </div>
          <div class="q-stat">
            <span class="q-k">有问题章节</span>
            <b>{{ fmt(report.summary.chapters_with_issues) }}</b>
          </div>
          <div class="q-stat">
            <span class="q-k">跨章重复</span>
            <b>{{ fmt(report.summary.duplicate_pairs) }}</b>
            <span class="hint">对</span>
          </div>
          <div class="q-stat">
            <span class="q-k">空章</span>
            <b>{{ fmt(report.summary.empty_chapters) }}</b>
          </div>
          <div class="q-stat">
            <span class="q-k">残留标记</span>
            <b>{{ fmt(report.summary.residual_markers) }}</b>
          </div>
          <div class="q-stat">
            <span class="q-k">占位/未完</span>
            <b>{{ fmt(report.summary.placeholder_hits) }}</b>
          </div>
          <div class="q-stat">
            <span class="q-k">禁用设定命中</span>
            <b>{{ fmt(report.summary.forbidden_hits) }}</b>
          </div>
          <div v-if="report.summary.old_name_hits" class="q-stat">
            <span class="q-k">改名残留</span>
            <b>{{ fmt(report.summary.old_name_hits) }}</b>
          </div>
          <div v-if="report.summary.auto_rewrites" class="q-stat">
            <span class="q-k">自动重写</span>
            <b>{{ fmt(report.summary.auto_rewrites) }}</b>
            <span class="hint">批</span>
          </div>
          <div v-if="report.summary.stitch_failures" class="q-stat">
            <span class="q-k">缝合跳过</span>
            <b>{{ fmt(report.summary.stitch_failures) }}</b>
            <span class="hint">处</span>
          </div>
        </div>

        <h4 class="q-h4">问题清单</h4>
        <div v-if="!report.issues.length" class="log-empty">未发现结构性问题</div>
        <div v-else class="sum-list">
          <div v-for="(it, i) in report.issues" :key="i" class="split-row q-issue">
            <div class="q-issue-main">
              <div>
                <span :class="it.severity === 'bad' ? 'badge err2' : 'badge paused'">
                  {{ it.severity === 'bad' ? '硬性' : '留意' }}
                </span>
                <span class="q-issue-msg">{{ it.message }}</span>
              </div>
              <div v-if="it.evidence" class="hint mono q-evidence">{{ it.evidence }}</div>
            </div>
            <span v-if="it.chapter_id" class="hint mono">章 {{ it.chapter_id }}</span>
          </div>
        </div>
        <p v-if="report.issues_truncated" class="hint">清单过长，仅显示前 200 条。</p>

        <template v-if="report.duplicates.length">
          <h4 class="q-h4">跨章重复</h4>
          <div class="sum-list">
            <div v-for="(d, i) in report.duplicates" :key="i" class="split-row q-dup">
              <span>第 {{ d.a }} 章 ↔ 第 {{ d.b }} 章</span>
              <span class="hint">
                共享 {{ d.shared_shingles }} 处 60 字片段 · 占较短章
                {{ (d.containment * 100).toFixed(1) }}%
              </span>
            </div>
          </div>
        </template>

        <div class="q-h4-row">
          <h4 class="q-h4">章节明细</h4>
          <button class="min-btn" @click="showAll = !showAll">
            {{ showAll ? '只看异常章节' : `显示全部 ${report.chapters.length} 章` }}
          </button>
        </div>
        <div v-if="!visibleChapters.length" class="log-empty">
          所有章节的篇幅都在自动检查的合理范围内
        </div>
        <div v-else class="q-table-wrap">
          <table class="q-table">
            <thead>
              <tr>
                <th>章</th>
                <th>标题</th>
                <th>原文</th>
                <th>重构</th>
                <th>比例</th>
                <th>状态</th>
                <th>问题</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in visibleChapters" :key="row.id">
                <td class="mono">{{ row.id }}</td>
                <td class="q-title">{{ row.title }}</td>
                <td>{{ fmt(row.original_chars) }}</td>
                <td>{{ fmt(row.reconstructed_chars) }}</td>
                <td>{{ ratioText(row.ratio) }}</td>
                <td>
                  <span :class="rowBadge(row)">{{ rowStatus(row) }}</span>
                </td>
                <td class="mono q-codes">{{ row.issue_codes.join('、') || '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <p class="hint q-caveat">{{ report.caveat }}</p>
        <p class="hint q-config">
          {{ report.config.model || '未知模型' }} · {{ report.config.refactor_mode || '-' }} ·
          提示词
          {{ report.config.prompt_version || '-' }}
        </p>
      </template>

      <div class="modal-actions">
        <span></span>
        <button type="button" @click="emit('close')">关闭</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.q-modal {
  width: min(1040px, 94vw);
}
.q-headline {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 8px 0 12px;
}
.q-headline-text {
  font-size: 14px;
}
.q-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 8px;
}
.q-stat {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.q-stat b {
  font-size: 18px;
}
.q-k {
  color: #6b7280;
  font-size: 12px;
}
.q-h4 {
  margin: 18px 0 4px;
  font-size: 14px;
}
.q-h4-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.q-issue {
  display: flex;
  justify-content: space-between;
  gap: 10px;
}
.q-issue-main {
  min-width: 0;
}
.q-issue-msg {
  margin-left: 6px;
  font-size: 13px;
}
.q-evidence {
  margin-top: 4px;
  font-size: 11px;
  color: #6b7280;
  word-break: break-all;
}
.q-dup {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 13px;
}
.q-table-wrap {
  max-height: 320px;
  overflow: auto;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
}
.q-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.q-table th,
.q-table td {
  padding: 6px 8px;
  border-bottom: 1px solid #f3f4f6;
  text-align: left;
  white-space: nowrap;
}
.q-table th {
  position: sticky;
  top: 0;
  background: #f9fafb;
  color: #6b7280;
  font-weight: 600;
}
.q-title {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.q-codes {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  color: #6b7280;
}
.q-caveat {
  margin-top: 14px;
}
.q-config {
  margin-top: 2px;
}
</style>
