<script setup lang="ts">
/**
 * 三栏「边跑边审」流式章节审阅工作台（ChapterReviewWorkbench）。
 *
 * 布局：
 * - 左栏：章节过滤树（全部 / 待审阅 / 有质检提示 / 已通过）+ 实时「边跑边审：已完成 X/Y 章」进度徽章
 * - 中栏：三种检视/精修模式切换（「句级对照 Diff」 / 「双栏并排」 / 「内联精修编辑」直接改写并重跑质检）
 * - 右栏：单章结构性质检诊断卡片、字数增减与改动率、重写次数徽章、「✓ 通过本章」与「↻ 重写本章」联动
 */
import { computed, ref, watch } from 'vue';
import {
  computeInlineDiffSummary,
  fetchChapterDiff,
  fetchChapterList,
  filterChaptersForWorkbench,
  submitChapterReview,
  type ChapterDiffResponse,
  type ChapterFilterTab,
  type ChapterSummaryItem,
  type WorkbenchCenterMode,
} from '../api/chapters';

const props = defineProps<{
  port: number | null;
  outputPath: string;
  runId: string | null;
  pipelineRunning: boolean;
  /** 外部递增信号（如 batch_stream_ready / 单章重写完成 / 阶段切换），触发章节列表静默刷新 */
  refreshTick: number;
  regeneratingIds: Set<string>;
}>();

const emit = defineEmits<{
  (e: 'regen', chapter: { id: string; chapter_num: number; title: string }): void;
  (e: 'log', message: string, level?: 'info' | 'ok' | 'warn' | 'err' | 'accent'): void;
  (e: 'error', message: string): void;
}>();

const collapsed = ref(false);
const filterTab = ref<ChapterFilterTab>('all');
const centerMode = ref<WorkbenchCenterMode>('diff');

const loadingList = ref(false);
const loadingDiff = ref(false);
const savingReview = ref(false);

const chapters = ref<ChapterSummaryItem[]>([]);
const totalChapters = ref(0);
const readyCount = ref(0);
const approvedCount = ref(0);
const editedCount = ref(0);
const flaggedCount = ref(0);

const selectedChapterNum = ref<number | null>(null);
const currentDiff = ref<ChapterDiffResponse | null>(null);
const draftText = ref('');
const localNotice = ref('');

const FILTER_TABS: { key: ChapterFilterTab; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'pending', label: '待审阅' },
  { key: 'qc', label: '有质检提示' },
  { key: 'approved', label: '已通过' },
];

const CENTER_MODES: { key: WorkbenchCenterMode; label: string }[] = [
  { key: 'diff', label: '句级对照 Diff' },
  { key: 'side_by_side', label: '双栏并排' },
  { key: 'edit', label: '内联精修编辑' },
];

const filteredChapters = computed(() =>
  filterChaptersForWorkbench(chapters.value, filterTab.value),
);

const selectedSummary = computed(
  () => chapters.value.find((c) => c.chapter_num === selectedChapterNum.value) ?? null,
);

const isEditDirty = computed(() => {
  if (!currentDiff.value) return false;
  return draftText.value !== currentDiff.value.refactored_text;
});

const liveEditStats = computed(() => {
  if (!currentDiff.value) return null;
  return computeInlineDiffSummary(currentDiff.value.original_text, draftText.value);
});

const activeRegenerating = computed(() => {
  const cid = currentDiff.value?.chapter_id || selectedSummary.value?.chapter_id;
  return cid ? props.regeneratingIds.has(String(cid)) : false;
});

function fmtDelta(delta: number): string {
  if (delta > 0) return `+${delta.toLocaleString('zh-CN')}`;
  return delta.toLocaleString('zh-CN');
}

function fmtPct(ratio: number | null | undefined): string {
  if (ratio == null || !Number.isFinite(ratio)) return '-';
  return `${(ratio * 100).toFixed(1)}%`;
}

function reviewBadgeText(c: ChapterSummaryItem): string {
  if (c.status !== 'ready_for_review') return '待重构';
  if (c.review_status === 'approved') return '✓ 已通过';
  if (c.review_status === 'edited') return '✎ 已精修';
  return '待审阅';
}

function reviewBadgeClass(c: ChapterSummaryItem): string {
  if (c.status !== 'ready_for_review') return 'wb-pill pending';
  if (c.review_status === 'approved') return 'wb-pill ok';
  if (c.review_status === 'edited') return 'wb-pill edited';
  return 'wb-pill ready';
}

async function reloadList(autoSelectFirstReady = false) {
  if (!props.port || !props.outputPath) return;
  loadingList.value = true;
  try {
    const data = await fetchChapterList(props.port, props.outputPath);
    chapters.value = data.chapters || [];
    totalChapters.value = data.total || 0;
    readyCount.value = data.ready_count || 0;
    approvedCount.value = data.approved_count || 0;
    editedCount.value = data.edited_count || 0;
    flaggedCount.value = data.flagged_count || 0;

    if (selectedChapterNum.value == null || autoSelectFirstReady) {
      const firstReady = chapters.value.find((c) => c.status === 'ready_for_review');
      const fallback = chapters.value[0];
      const pick = firstReady ?? fallback;
      if (pick && selectedChapterNum.value !== pick.chapter_num) {
        selectedChapterNum.value = pick.chapter_num;
        await loadChapterDiff(pick.chapter_num);
      }
    } else if (
      selectedChapterNum.value != null &&
      !isEditDirty.value &&
      currentDiff.value?.chapter_num === selectedChapterNum.value
    ) {
      // 若当前选中章在后台刚产出重构稿而此前为空，自动补拉一次正文对照
      const curRow = chapters.value.find((c) => c.chapter_num === selectedChapterNum.value);
      if (
        curRow &&
        curRow.status === 'ready_for_review' &&
        !currentDiff.value.refactored_text &&
        curRow.char_refactored > 0
      ) {
        await loadChapterDiff(selectedChapterNum.value);
      }
    }
  } catch {
    /* 尚未生成章节表时静默忽略 */
  } finally {
    loadingList.value = false;
  }
}

async function loadChapterDiff(chapterNum: number) {
  if (!props.port || !props.outputPath) return;
  loadingDiff.value = true;
  localNotice.value = '';
  try {
    const d = await fetchChapterDiff(props.port, chapterNum, props.outputPath);
    currentDiff.value = d;
    draftText.value = d.refactored_text || '';
  } catch (e: any) {
    emit('error', `读取第 ${chapterNum} 章对照失败：${e?.message || e}`);
  } finally {
    loadingDiff.value = false;
  }
}

async function selectChapter(c: ChapterSummaryItem) {
  if (isEditDirty.value) {
    const ok = window.confirm('当前章节有未保存的精修内容，切换章节将丢弃草稿，确定继续吗？');
    if (!ok) return;
  }
  selectedChapterNum.value = c.chapter_num;
  await loadChapterDiff(c.chapter_num);
}

async function markApproval(nextStatus: 'approved' | 'pending') {
  if (!props.port || !currentDiff.value || savingReview.value) return;
  const cnum = currentDiff.value.chapter_num;
  savingReview.value = true;
  localNotice.value = '';
  try {
    const res = await submitChapterReview(
      props.port,
      cnum,
      {
        output_path: props.outputPath,
        run_id: props.runId,
        review_status: nextStatus,
      },
      {
        onBusy: (n) => emit('log', `文件正被占用，审阅状态保存自动重试第 ${n} 次…`, 'warn'),
      },
    );
    currentDiff.value.review_status = res.review_status;
    localNotice.value = nextStatus === 'approved' ? '已标记本章审阅通过' : '已恢复为待审阅';
    emit(
      'log',
      `第 ${cnum} 章审阅状态已更新为：${nextStatus === 'approved' ? '已通过' : '待审阅'}`,
      'ok',
    );
    await reloadList(false);
  } catch (e: any) {
    emit('error', `更新审阅状态失败：${e?.message || e}`);
  } finally {
    savingReview.value = false;
  }
}

async function saveInlineEdit() {
  if (!props.port || !currentDiff.value || savingReview.value) return;
  if (!draftText.value.trim()) {
    emit('error', '精修正文不能为空');
    return;
  }
  const cnum = currentDiff.value.chapter_num;
  savingReview.value = true;
  localNotice.value = '';
  try {
    const res = await submitChapterReview(
      props.port,
      cnum,
      {
        output_path: props.outputPath,
        run_id: props.runId,
        review_status: 'edited',
        edited_text: draftText.value,
      },
      {
        onBusy: (n) => emit('log', `文件正被占用，保存精修稿自动重试第 ${n} 次…`, 'warn'),
      },
    );
    localNotice.value = `精修已保存并完成单章质检（问题 ${res.qc_issues.length} 项）`;
    emit(
      'log',
      `第 ${cnum} 章人工精修已落盘并刷新质检（遗留问题 ${res.qc_issues.length} 项）`,
      'ok',
    );
    await loadChapterDiff(cnum);
    await reloadList(false);
  } catch (e: any) {
    emit('error', `保存精修失败：${e?.message || e}`);
  } finally {
    savingReview.value = false;
  }
}

function triggerRegen() {
  const row = selectedSummary.value;
  if (!row) return;
  emit('regen', {
    id: row.chapter_id || String(row.chapter_num),
    chapter_num: row.chapter_num,
    title: row.title,
  });
}

watch(
  () => [props.port, props.outputPath],
  ([p, out]) => {
    if (p && out) {
      selectedChapterNum.value = null;
      currentDiff.value = null;
      void reloadList(true);
    }
  },
  { immediate: true },
);

watch(
  () => props.refreshTick,
  async () => {
    await reloadList(false);
    if (selectedChapterNum.value != null && !isEditDirty.value) {
      await loadChapterDiff(selectedChapterNum.value);
    }
  },
);
</script>

<template>
  <section class="panel wb-panel" aria-label="边跑边审章节工作台">
    <div class="wb-header">
      <div class="wb-title-group">
        <h2>边跑边审 · 章节对照与精修工作台</h2>
        <span class="wb-live-badge">
          边跑边审：已完成 {{ readyCount }}/{{ totalChapters }} 章
        </span>
        <span v-if="approvedCount + editedCount > 0" class="wb-sub-badge ok">
          已审定 {{ approvedCount + editedCount }} 章
        </span>
        <span v-if="flaggedCount > 0" class="wb-sub-badge warn">
          质检提示 {{ flaggedCount }} 章
        </span>
      </div>
      <div class="wb-header-actions">
        <button
          type="button"
          class="wb-btn ghost"
          :disabled="loadingList"
          @click="reloadList(false)"
        >
          {{ loadingList ? '刷新中…' : '↻ 刷新列表' }}
        </button>
        <button type="button" class="wb-btn ghost" @click="collapsed = !collapsed">
          {{ collapsed ? '展开工作台' : '收起工作台' }}
        </button>
      </div>
    </div>

    <div v-if="!collapsed" class="wb-grid">
      <!-- 左栏：可筛选章节树 -->
      <aside class="wb-col wb-left">
        <div class="wb-tabs" role="tablist" aria-label="章节筛选">
          <button
            v-for="tab in FILTER_TABS"
            :key="tab.key"
            type="button"
            class="wb-tab"
            :class="{ active: filterTab === tab.key }"
            @click="filterTab = tab.key"
          >
            {{ tab.label }}
          </button>
        </div>

        <div v-if="!filteredChapters.length" class="wb-empty">
          {{ chapters.length ? '当前筛选下无章节' : '等待首批章节产出…' }}
        </div>
        <div v-else class="wb-chapter-list">
          <button
            v-for="ch in filteredChapters"
            :key="ch.chapter_num"
            type="button"
            class="wb-chapter-item"
            :class="{ selected: ch.chapter_num === selectedChapterNum }"
            @click="selectChapter(ch)"
          >
            <div class="wb-ch-top">
              <span class="wb-ch-num">#{{ ch.chapter_num }}</span>
              <span class="wb-ch-title">{{ ch.title }}</span>
              <span v-if="ch.qc_issues.length" class="wb-qc-dot" title="有结构性质检提示">!</span>
            </div>
            <div class="wb-ch-meta">
              <span :class="reviewBadgeClass(ch)">{{ reviewBadgeText(ch) }}</span>
              <span v-if="ch.char_refactored > 0" class="wb-chars">
                {{ ch.char_raw }} → {{ ch.char_refactored }} 字
              </span>
            </div>
          </button>
        </div>
      </aside>

      <!-- 中栏：句级 Diff / 双栏并排 / 内联精修编辑 -->
      <div class="wb-col wb-center">
        <div class="wb-mode-bar">
          <div class="wb-modes">
            <button
              v-for="m in CENTER_MODES"
              :key="m.key"
              type="button"
              class="wb-mode-btn"
              :class="{ active: centerMode === m.key }"
              @click="centerMode = m.key"
            >
              {{ m.label }}
            </button>
          </div>
          <span v-if="localNotice" class="wb-notice">{{ localNotice }}</span>
        </div>

        <div v-if="loadingDiff" class="wb-empty wb-center-empty">正在加载章节对照…</div>
        <div v-else-if="!currentDiff" class="wb-empty wb-center-empty">
          请从左侧选择一章查看句级对照或内联精修
        </div>
        <div
          v-else-if="!currentDiff.refactored_text && centerMode !== 'edit'"
          class="wb-empty wb-center-empty"
        >
          本章尚未完成重构，正在等待流水线批次产出（原文共
          {{ currentDiff.stats.char_original }} 字）
        </div>

        <!-- 模式 1：句级对照 Diff -->
        <div v-else-if="centerMode === 'diff'" class="wb-diff-scroll">
          <div
            v-for="(blk, idx) in currentDiff.diff_blocks"
            :key="idx"
            class="wb-diff-block"
            :class="`op-${blk.op}`"
          >
            <template v-if="blk.op === 'equal'">
              <span class="wb-seg-equal">{{ blk.refactored_text }}</span>
            </template>
            <template v-else-if="blk.op === 'delete'">
              <del class="wb-seg-del">{{ blk.original_text }}</del>
            </template>
            <template v-else-if="blk.op === 'insert'">
              <ins class="wb-seg-ins">{{ blk.refactored_text }}</ins>
            </template>
            <template v-else>
              <div class="wb-seg-replace">
                <del class="wb-seg-del">{{ blk.original_text }}</del>
                <ins class="wb-seg-ins">{{ blk.refactored_text }}</ins>
              </div>
            </template>
          </div>
        </div>

        <!-- 模式 2：双栏并排 -->
        <div v-else-if="centerMode === 'side_by_side'" class="wb-side-grid">
          <div class="wb-pane">
            <div class="wb-pane-head">原文（{{ currentDiff.stats.char_original }} 字）</div>
            <pre class="wb-pane-body">{{ currentDiff.original_text }}</pre>
          </div>
          <div class="wb-pane">
            <div class="wb-pane-head">重构稿（{{ currentDiff.stats.char_refactored }} 字）</div>
            <pre class="wb-pane-body">{{ currentDiff.refactored_text }}</pre>
          </div>
        </div>

        <!-- 模式 3：内联精修编辑 -->
        <div v-else class="wb-edit-wrap">
          <textarea
            v-model="draftText"
            class="wb-editor"
            placeholder="在此直接精修本章正文，保存后将原子写入重构稿并立即重跑单章结构性质检…"
          ></textarea>
          <div class="wb-edit-foot">
            <span v-if="liveEditStats" class="wb-edit-stat">
              当前草稿 {{ liveEditStats.charEdited }} 字（相对原文
              {{ fmtDelta(liveEditStats.charDelta) }} 字 · 比例
              {{ fmtPct(liveEditStats.lengthRatio) }} · 估算句级改动
              {{ fmtPct(liveEditStats.changeRatio) }}）
              <strong v-if="isEditDirty" class="wb-dirty-flag">· 未保存修改</strong>
            </span>
            <div class="wb-edit-actions">
              <button
                v-if="isEditDirty"
                type="button"
                class="wb-btn ghost"
                :disabled="savingReview"
                @click="draftText = currentDiff.refactored_text"
              >
                放弃草稿
              </button>
              <button
                type="button"
                class="wb-btn primary"
                :disabled="savingReview || !draftText.trim()"
                @click="saveInlineEdit"
              >
                {{ savingReview ? '保存质检中…' : '💾 保存精修并重跑质检' }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- 右栏：章节质检诊断卡片、字数增减、重写计数与操作按钮 -->
      <aside class="wb-col wb-right">
        <template v-if="currentDiff">
          <div class="wb-card">
            <div class="wb-card-title">第 {{ currentDiff.chapter_num }} 章 · 篇幅与改动</div>
            <div class="wb-kv">
              <span>章节标题</span>
              <b>{{ currentDiff.title }}</b>
            </div>
            <div class="wb-kv">
              <span>字数变化</span>
              <b>
                {{ currentDiff.stats.char_original }} → {{ currentDiff.stats.char_refactored }}（{{
                  fmtDelta(currentDiff.stats.char_delta)
                }}）
              </b>
            </div>
            <div class="wb-kv">
              <span>篇幅比例 / 改动率</span>
              <b>
                {{ fmtPct(currentDiff.stats.length_ratio) }} /
                {{ fmtPct(currentDiff.stats.change_ratio) }}
              </b>
            </div>
            <div class="wb-kv">
              <span>单章重写次数</span>
              <span class="wb-regen-badge">已重写 {{ currentDiff.regen_count }} / 3 次</span>
            </div>
          </div>

          <div class="wb-card wb-qc-card">
            <div class="wb-card-title">结构性质检诊断</div>
            <div v-if="!currentDiff.qc_issues.length" class="wb-qc-clean">
              ✓ 未检出空章、塌缩、膨胀、占位或机器标记残留
            </div>
            <div v-else class="wb-qc-list">
              <div
                v-for="(iss, idx) in currentDiff.qc_issues"
                :key="idx"
                class="wb-qc-item"
                :class="iss.severity === 'bad' ? 'bad' : 'warn'"
              >
                <div class="wb-qc-head">
                  <span class="wb-qc-sev">{{ iss.severity === 'bad' ? '硬性' : '留意' }}</span>
                  <span>{{ iss.message }}</span>
                </div>
                <div v-if="iss.evidence" class="wb-qc-ev">{{ iss.evidence }}</div>
              </div>
            </div>
          </div>

          <div class="wb-actions-stack">
            <button
              v-if="currentDiff.review_status !== 'approved'"
              type="button"
              class="wb-btn approve"
              :disabled="savingReview || currentDiff.status !== 'ready_for_review'"
              @click="markApproval('approved')"
            >
              ✓ 通过本章
            </button>
            <button
              v-else
              type="button"
              class="wb-btn ghost"
              :disabled="savingReview"
              @click="markApproval('pending')"
            >
              ↺ 撤回为待审阅
            </button>

            <button
              type="button"
              class="wb-btn regen"
              :disabled="
                !pipelineRunning ||
                activeRegenerating ||
                currentDiff.status !== 'ready_for_review' ||
                currentDiff.regen_count >= 3
              "
              :title="
                !pipelineRunning
                  ? '模型重写仅在流水线运行期可用（API Key 仅驻留运行期内存）；结束后可使用「内联精修编辑」直接改稿'
                  : '调用当前重构模型重新生成本章正文'
              "
              @click="triggerRegen"
            >
              {{ activeRegenerating ? '↻ 重写中…' : '↻ 重写本章' }}
            </button>
          </div>
        </template>
        <div v-else class="wb-empty">选择章节后查看诊断与操作</div>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.wb-panel {
  grid-column: 1 / -1;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.wb-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.wb-title-group {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.wb-title-group h2 {
  margin: 0;
  font-size: 15px;
}
.wb-live-badge {
  background: #ebf8ff;
  color: #2b6cb0;
  border: 1px solid #bee3f8;
  border-radius: 999px;
  padding: 2px 10px;
  font-size: 12px;
  font-weight: 600;
}
.wb-sub-badge {
  border-radius: 999px;
  padding: 2px 10px;
  font-size: 12px;
}
.wb-sub-badge.ok {
  background: #f0fff4;
  color: #276749;
  border: 1px solid #c6f6d5;
}
.wb-sub-badge.warn {
  background: #fffff0;
  color: #975a16;
  border: 1px solid #fefcbf;
}
.wb-header-actions {
  display: flex;
  gap: 8px;
}
.wb-grid {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr) 290px;
  gap: 12px;
  min-height: 430px;
  max-height: 560px;
}
.wb-col {
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  background: #f9fafb;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
.wb-tabs {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  border-bottom: 1px solid #e2e8f0;
  background: #edf2f7;
}
.wb-tab {
  border: none;
  background: transparent;
  padding: 7px 4px;
  font-size: 12px;
  color: #4a5568;
  cursor: pointer;
}
.wb-tab.active {
  background: #fff;
  color: #2b6cb0;
  font-weight: 600;
  box-shadow: inset 0 -2px 0 #3182ce;
}
.wb-chapter-list {
  flex: 1;
  overflow-y: auto;
  padding: 6px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.wb-chapter-item {
  text-align: left;
  border: 1px solid transparent;
  background: #fff;
  border-radius: 5px;
  padding: 7px 9px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.wb-chapter-item:hover {
  border-color: #cbd5e0;
}
.wb-chapter-item.selected {
  border-color: #3182ce;
  background: #ebf8ff;
}
.wb-ch-top {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.wb-ch-num {
  color: #718096;
  font-family: 'Consolas', monospace;
}
.wb-ch-title {
  flex: 1;
  font-weight: 600;
  color: #2d3748;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wb-qc-dot {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #e53e3e;
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.wb-ch-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
}
.wb-pill {
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
}
.wb-pill.pending {
  background: #edf2f7;
  color: #718096;
}
.wb-pill.ready {
  background: #ebf8ff;
  color: #2b6cb0;
}
.wb-pill.ok {
  background: #c6f6d5;
  color: #22543d;
}
.wb-pill.edited {
  background: #e9d8fd;
  color: #553c9a;
}
.wb-chars {
  color: #718096;
}
.wb-mode-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  border-bottom: 1px solid #e2e8f0;
  background: #edf2f7;
  gap: 8px;
}
.wb-modes {
  display: flex;
  gap: 6px;
}
.wb-mode-btn {
  padding: 4px 10px;
  font-size: 12px;
  border: 1px solid #cbd5e0;
  background: #fff;
  color: #4a5568;
  border-radius: 4px;
  cursor: pointer;
}
.wb-mode-btn.active {
  background: #3182ce;
  border-color: #3182ce;
  color: #fff;
}
.wb-notice {
  font-size: 12px;
  color: #2f855a;
}
.wb-diff-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 12px 14px;
  background: #fff;
  font-size: 13px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-word;
}
.wb-diff-block {
  display: inline;
}
.wb-seg-del {
  background: #fed7d7;
  color: #9b2c2c;
  text-decoration: line-through;
  padding: 0 2px;
  border-radius: 2px;
}
.wb-seg-ins {
  background: #c6f6d5;
  color: #22543d;
  text-decoration: none;
  padding: 0 2px;
  border-radius: 2px;
}
.wb-seg-replace {
  display: inline;
}
.wb-side-grid {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  min-height: 0;
  background: #fff;
}
.wb-pane {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-right: 1px solid #e2e8f0;
}
.wb-pane:last-child {
  border-right: none;
}
.wb-pane-head {
  padding: 5px 10px;
  font-size: 12px;
  font-weight: 600;
  color: #4a5568;
  background: #f7fafc;
  border-bottom: 1px solid #e2e8f0;
}
.wb-pane-body {
  flex: 1;
  margin: 0;
  padding: 10px 12px;
  overflow-y: auto;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.wb-edit-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 8px;
  gap: 8px;
  background: #fff;
}
.wb-editor {
  flex: 1;
  width: 100%;
  resize: none;
  padding: 10px 12px;
  border: 1px solid #cbd5e0;
  border-radius: 5px;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  box-sizing: border-box;
}
.wb-edit-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.wb-edit-stat {
  font-size: 12px;
  color: #4a5568;
}
.wb-dirty-flag {
  color: #c05621;
}
.wb-edit-actions {
  display: flex;
  gap: 8px;
}
.wb-right {
  padding: 8px;
  gap: 8px;
  overflow-y: auto;
}
.wb-card {
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 5px;
  padding: 9px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.wb-card-title {
  font-size: 12px;
  font-weight: 700;
  color: #2d3748;
  border-bottom: 1px solid #edf2f7;
  padding-bottom: 4px;
}
.wb-kv {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #4a5568;
}
.wb-kv b {
  color: #1a202c;
  text-align: right;
}
.wb-regen-badge {
  background: #edf2f7;
  color: #2d3748;
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 11px;
  font-weight: 600;
}
.wb-qc-card {
  flex: 1;
  min-height: 110px;
}
.wb-qc-clean {
  font-size: 12px;
  color: #276749;
  background: #f0fff4;
  border: 1px solid #c6f6d5;
  border-radius: 4px;
  padding: 8px;
}
.wb-qc-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow-y: auto;
}
.wb-qc-item {
  border-radius: 4px;
  padding: 6px 8px;
  font-size: 12px;
}
.wb-qc-item.bad {
  background: #fff5f5;
  border: 1px solid #feb2b2;
  color: #822727;
}
.wb-qc-item.warn {
  background: #fffff0;
  border: 1px solid #f6e05e;
  color: #744210;
}
.wb-qc-head {
  display: flex;
  gap: 6px;
  align-items: baseline;
}
.wb-qc-sev {
  font-weight: 700;
  flex-shrink: 0;
}
.wb-qc-ev {
  margin-top: 3px;
  font-size: 11px;
  opacity: 0.85;
  word-break: break-all;
}
.wb-actions-stack {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.wb-btn {
  padding: 6px 12px;
  border-radius: 5px;
  font-size: 12px;
  cursor: pointer;
  border: 1px solid #cbd5e0;
  background: #fff;
  color: #2d3748;
}
.wb-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.wb-btn.primary {
  background: #3182ce;
  border-color: #3182ce;
  color: #fff;
}
.wb-btn.approve {
  background: #2f855a;
  border-color: #2f855a;
  color: #fff;
  font-weight: 600;
}
.wb-btn.regen {
  background: #fff;
  border-color: #805ad5;
  color: #6b46c1;
  font-weight: 600;
}
.wb-empty {
  padding: 18px 12px;
  text-align: center;
  font-size: 12px;
  color: #718096;
}
.wb-center-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #fff;
}

@media (max-width: 1100px) {
  .wb-grid {
    grid-template-columns: 1fr;
    max-height: none;
  }
}

html.dark .wb-col,
html.dark .wb-card,
html.dark .wb-diff-scroll,
html.dark .wb-side-grid,
html.dark .wb-edit-wrap,
html.dark .wb-center-empty {
  background: #1a202c;
  border-color: #4a5568;
  color: #e2e8f0;
}
html.dark .wb-tabs,
html.dark .wb-mode-bar,
html.dark .wb-pane-head {
  background: #232b39;
  border-color: #4a5568;
  color: #cbd5e0;
}
html.dark .wb-tab {
  color: #a0aec0;
}
html.dark .wb-tab.active {
  background: #1a202c;
  color: #90cdf4;
}
html.dark .wb-chapter-item {
  background: #232b39;
}
html.dark .wb-chapter-item.selected {
  background: #2a4365;
  border-color: #63b3ed;
}
html.dark .wb-ch-title,
html.dark .wb-card-title,
html.dark .wb-kv b {
  color: #e2e8f0;
}
html.dark .wb-kv,
html.dark .wb-edit-stat {
  color: #a0aec0;
}
html.dark .wb-btn,
html.dark .wb-mode-btn {
  background: #2d3748;
  border-color: #4a5568;
  color: #e2e8f0;
}
html.dark .wb-mode-btn.active,
html.dark .wb-btn.primary {
  background: #3182ce;
  border-color: #3182ce;
  color: #fff;
}
html.dark .wb-btn.approve {
  background: #2f855a;
  border-color: #2f855a;
  color: #fff;
}
html.dark .wb-seg-del {
  background: rgba(245, 101, 101, 0.25);
  color: #feb2b2;
}
html.dark .wb-seg-ins {
  background: rgba(72, 187, 120, 0.25);
  color: #9ae6b4;
}
</style>
