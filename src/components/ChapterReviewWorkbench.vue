<script setup lang="ts">
/**
 * 三栏「边跑边审」流式章节审阅工作台（ChapterReviewWorkbench）。
 *
 * 布局：
 * - 左栏：章节过滤树（全部 / 待审阅 / 有质检提示 / 已通过）+ 实时「边跑边审：已完成 X/Y 章」进度徽章
 * - 中栏：三种检视/精修模式切换（「句级对照 Diff」 / 「双栏并排」 / 「内联精修编辑」直接改写并重跑质检）
 * - 右栏：单章结构性质检诊断卡片、字数增减与改动率、重写次数徽章、「✓ 通过本章」与「↻ 重写本章」联动
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import {
  computeInlineDiffSummary,
  fetchChapterDiff,
  fetchChapterList,
  fetchChaptersAudit,
  filterChaptersForWorkbench,
  resolveWorkbenchShortcut,
  submitChapterReview,
  type ChapterAuditItem,
  type ChapterAuditSummary,
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
  (e: 'review-updated'): void;
  (e: 'log', message: string, level?: 'info' | 'ok' | 'warn' | 'err' | 'accent'): void;
  (e: 'error', message: string): void;
}>();

const collapsed = ref(false);
const filterTab = ref<ChapterFilterTab>('all');
const centerMode = ref<WorkbenchCenterMode>('diff');

const loadingList = ref(false);
const loadingDiff = ref(false);
const savingReview = ref(false);
const auditing = ref(false);

const chapters = ref<ChapterSummaryItem[]>([]);
const totalChapters = ref(0);
const readyCount = ref(0);
const approvedCount = ref(0);
const editedCount = ref(0);
const flaggedCount = ref(0);

const searchQuery = ref('');
const activeSearchQuery = ref('');
const auditSummary = ref<ChapterAuditSummary | null>(null);
const auditItemsByNum = ref<Map<number, ChapterAuditItem>>(new Map());
const searchMatchedNums = ref<Set<number> | null>(null);

const selectedChapterNum = ref<number | null>(null);
const currentDiff = ref<ChapterDiffResponse | null>(null);
const draftText = ref('');
const localNotice = ref('');

const FILTER_TABS: { key: ChapterFilterTab; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'pending', label: '待审阅' },
  { key: 'qc', label: '质检提示' },
  { key: 'audit', label: '巡检异常' },
  { key: 'approved', label: '已通过' },
];

const CENTER_MODES: { key: WorkbenchCenterMode; label: string }[] = [
  { key: 'diff', label: '句级对照 Diff' },
  { key: 'side_by_side', label: '双栏并排' },
  { key: 'edit', label: '内联精修编辑' },
];

const auditFlaggedNums = computed(() => {
  const nums = new Set<number>();
  for (const item of auditItemsByNum.value.values()) {
    if (item.findings.length > 0) {
      nums.add(item.chapter_num);
    }
  }
  return nums;
});

const filteredChapters = computed(() =>
  filterChaptersForWorkbench(chapters.value, filterTab.value, {
    auditFlaggedNums: auditFlaggedNums.value,
    searchMatchedNums: searchMatchedNums.value,
  }),
);

const selectedAuditItem = computed(() =>
  selectedChapterNum.value != null
    ? (auditItemsByNum.value.get(selectedChapterNum.value) ?? null)
    : null,
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

const lengthRatioWidth = computed(() => {
  const r = currentDiff.value?.stats.length_ratio;
  if (r == null || !Number.isFinite(r) || r <= 0) return '0%';
  return `${Math.min(100, Math.round((r / 1.5) * 100))}%`;
});

const lengthRatioHealthy = computed(() => {
  const r = currentDiff.value?.stats.length_ratio;
  if (r == null || !Number.isFinite(r)) return true;
  return r >= 0.6 && r <= 1.45;
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
    emit('review-updated');
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
    emit('review-updated');
    await loadChapterDiff(cnum);
    await reloadList(false);
  } catch (e: any) {
    emit('error', `保存精修失败：${e?.message || e}`);
  } finally {
    savingReview.value = false;
  }
}

async function runAuditOrSearch(opts: { switchToAuditTab?: boolean; queryOverride?: string } = {}) {
  if (!props.port || !props.outputPath || auditing.value) return;
  const q = opts.queryOverride !== undefined ? opts.queryOverride.trim() : searchQuery.value.trim();
  auditing.value = true;
  try {
    const res = await fetchChaptersAudit(props.port, props.outputPath, q || undefined);
    auditSummary.value = res.summary;
    const map = new Map<number, ChapterAuditItem>();
    for (const item of res.items) {
      map.set(item.chapter_num, item);
    }
    auditItemsByNum.value = map;

    if (q) {
      activeSearchQuery.value = q;
      searchMatchedNums.value = new Set(res.items.map((it) => it.chapter_num));
      localNotice.value = `全书检索「${q}」命中 ${res.summary.search_matched_chapters} 章（共 ${res.summary.search_total_matches} 处）`;
    } else {
      activeSearchQuery.value = '';
      searchMatchedNums.value = null;
      localNotice.value = `全书巡检完成：共核查 ${res.summary.audited_chapters} 章，需关注 ${res.summary.flagged_chapters} 章`;
    }

    if (opts.switchToAuditTab) {
      filterTab.value = 'audit';
    }

    const visible = filteredChapters.value;
    if (
      visible.length > 0 &&
      !isEditDirty.value &&
      !visible.some((c) => c.chapter_num === selectedChapterNum.value)
    ) {
      selectedChapterNum.value = visible[0].chapter_num;
      await loadChapterDiff(visible[0].chapter_num);
    }
  } catch (e: any) {
    emit('error', `全书检索/设定巡检失败：${e?.message || e}`);
  } finally {
    auditing.value = false;
  }
}

function clearSearchAndAuditFilter() {
  searchQuery.value = '';
  activeSearchQuery.value = '';
  searchMatchedNums.value = null;
  if (filterTab.value === 'audit') {
    filterTab.value = 'all';
  }
}

async function stepChapter(delta: 1 | -1) {
  const list = filteredChapters.value;
  if (!list.length) return;
  const curIdx = list.findIndex((c) => c.chapter_num === selectedChapterNum.value);
  const nextIdx = curIdx < 0 ? 0 : Math.max(0, Math.min(list.length - 1, curIdx + delta));
  const target = list[nextIdx];
  if (target && target.chapter_num !== selectedChapterNum.value) {
    await selectChapter(target);
  }
}

function onWorkbenchKeydown(e: KeyboardEvent) {
  const t = e.target as HTMLElement | null;
  const action = resolveWorkbenchShortcut({
    key: e.key,
    ctrlKey: e.ctrlKey,
    metaKey: e.metaKey,
    altKey: e.altKey,
    shiftKey: e.shiftKey,
    repeat: e.repeat,
    targetTagName: t?.tagName ?? null,
    isContentEditable: Boolean(t?.isContentEditable),
    inEditMode: centerMode.value === 'edit',
    collapsed: collapsed.value,
  });
  if (!action) return;

  switch (action) {
    case 'save_edit':
      e.preventDefault();
      void saveInlineEdit();
      break;
    case 'next_chapter':
      e.preventDefault();
      void stepChapter(1);
      break;
    case 'prev_chapter':
      e.preventDefault();
      void stepChapter(-1);
      break;
    case 'toggle_approve':
      if (
        currentDiff.value &&
        currentDiff.value.status === 'ready_for_review' &&
        !savingReview.value
      ) {
        e.preventDefault();
        void markApproval(currentDiff.value.review_status === 'approved' ? 'pending' : 'approved');
      }
      break;
    case 'mode_diff':
      e.preventDefault();
      centerMode.value = 'diff';
      break;
    case 'mode_side_by_side':
      e.preventDefault();
      centerMode.value = 'side_by_side';
      break;
    case 'mode_edit':
      e.preventDefault();
      centerMode.value = 'edit';
      break;
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

onMounted(() => {
  window.addEventListener('keydown', onWorkbenchKeydown);
});

onUnmounted(() => {
  window.removeEventListener('keydown', onWorkbenchKeydown);
});

watch(
  () => [props.port, props.outputPath],
  ([p, out]) => {
    if (p && out) {
      selectedChapterNum.value = null;
      currentDiff.value = null;
      searchQuery.value = '';
      activeSearchQuery.value = '';
      searchMatchedNums.value = null;
      auditSummary.value = null;
      auditItemsByNum.value = new Map();
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
        <span class="wb-seal" aria-hidden="true">校</span>
        <h2>边跑边审 · 书卷对照与精修案台</h2>
        <span class="wb-live-badge tabular">
          边跑边审：已完成 {{ readyCount }}/{{ totalChapters }} 章
        </span>
        <span v-if="approvedCount + editedCount > 0" class="wb-sub-badge ok tabular">
          已审定 {{ approvedCount + editedCount }} 章
        </span>
        <span v-if="flaggedCount > 0" class="wb-sub-badge warn tabular">
          质检提示 {{ flaggedCount }} 章
        </span>
      </div>
      <div class="wb-header-actions">
        <span
          class="wb-shortcut-legend"
          title="仅在未聚焦输入框时响应单键；精修编辑框内支持 Ctrl+S"
        >
          快捷键：J/K 上下章 · 1/2/3 视图 · A 通过 · Ctrl+S 保存精修
        </span>
        <button
          type="button"
          class="wb-btn ghost"
          :disabled="auditing || !readyCount"
          title="全书扫描禁用设定泄露、未替换旧人名、极端压缩/膨胀与缝合边界告警"
          @click="runAuditOrSearch({ switchToAuditTab: true, queryOverride: '' })"
        >
          {{ auditing ? '巡检中…' : '🔍 设定与篇幅巡检' }}
        </button>
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
      <!-- 左栏：全书检索/设定一致性巡检 + 可筛选章节树 -->
      <aside class="wb-col wb-left">
        <div class="wb-search-bar" aria-label="全书正文检索 / 设定一致性巡检">
          <div class="wb-search-field">
            <span class="wb-search-icon" aria-hidden="true">⌕</span>
            <input
              v-model="searchQuery"
              type="search"
              class="wb-search-input"
              placeholder="检索人名 / 词条（回车）…"
              @keydown.enter.prevent="runAuditOrSearch()"
            />
            <button
              v-if="searchQuery || activeSearchQuery || filterTab === 'audit'"
              type="button"
              class="wb-search-clear"
              title="清除检索与巡检筛选"
              @click="clearSearchAndAuditFilter"
            >
              ×
            </button>
          </div>
          <button
            type="button"
            class="wb-search-btn"
            :disabled="auditing"
            @click="runAuditOrSearch()"
          >
            检索
          </button>
        </div>

        <div v-if="auditSummary" class="wb-audit-strip tabular">
          <template v-if="activeSearchQuery">
            检索「{{ activeSearchQuery }}」：命中 {{ auditSummary.search_matched_chapters }} 章 ·
            {{ auditSummary.search_total_matches }} 处
          </template>
          <template v-else>
            巡检 {{ auditSummary.audited_chapters }} 章：异常
            <b>{{ auditSummary.flagged_chapters }}</b> 章（禁设 {{ auditSummary.forbidden_hits }} ·
            旧名 {{ auditSummary.old_name_hits }} · 篇幅 {{ auditSummary.length_anomalies }} · 缝合
            {{ auditSummary.stitch_warnings }}）
          </template>
        </div>

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
              <span
                v-if="auditFlaggedNums.has(ch.chapter_num)"
                class="wb-audit-dot"
                title="设定/篇幅/缝合巡检提示"
              >
                巡
              </span>
            </div>
            <div class="wb-ch-meta">
              <span :class="reviewBadgeClass(ch)">{{ reviewBadgeText(ch) }}</span>
              <span v-if="ch.char_refactored > 0" class="wb-chars tabular">
                {{ ch.char_raw }} → {{ ch.char_refactored }} 字
              </span>
            </div>
            <div
              v-if="auditItemsByNum.get(ch.chapter_num)?.search_matches.snippets[0]"
              class="wb-ch-snip"
            >
              {{ auditItemsByNum.get(ch.chapter_num)?.search_matches.snippets[0] }}
            </div>
            <div
              v-else-if="auditItemsByNum.get(ch.chapter_num)?.findings[0]"
              class="wb-ch-snip warn"
            >
              {{ auditItemsByNum.get(ch.chapter_num)?.findings[0]?.message }}
            </div>
          </button>
        </div>
      </aside>

      <!-- 中栏：书卷手稿纸张（句级 Diff / 双栏并排 / 内联精修编辑） -->
      <div class="wb-col wb-center">
        <div class="wb-mode-bar">
          <div class="wb-modes">
            <button
              v-for="(m, mIdx) in CENTER_MODES"
              :key="m.key"
              type="button"
              class="wb-mode-btn"
              :class="{ active: centerMode === m.key }"
              @click="centerMode = m.key"
            >
              <kbd class="wb-mode-key">{{ mIdx + 1 }}</kbd>
              {{ m.label }}
            </button>
          </div>
          <span v-if="localNotice" class="wb-notice">{{ localNotice }}</span>
        </div>

        <div v-if="loadingDiff" class="wb-empty wb-center-empty">正在展卷加载章节对照…</div>
        <div v-else-if="!currentDiff" class="wb-empty wb-center-empty">
          请从左侧书目选择一章，展开句级批注或内联精修
        </div>
        <div
          v-else-if="!currentDiff.refactored_text && centerMode !== 'edit'"
          class="wb-empty wb-center-empty tabular"
        >
          本章尚未完成重构，正在等待流水线批次产出（原文共
          {{ currentDiff.stats.char_original }} 字）
        </div>

        <!-- 模式 1：句级对照 Diff（书卷批注模式） -->
        <div v-else-if="centerMode === 'diff'" class="wb-diff-scroll">
          <div class="wb-manuscript-head">
            <span class="wb-ms-num mono">CHAPTER {{ currentDiff.chapter_num }}</span>
            <h3 class="wb-ms-title">{{ currentDiff.title }}</h3>
          </div>
          <div class="wb-manuscript-body">
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
        </div>

        <!-- 模式 2：双栏并排 -->
        <div v-else-if="centerMode === 'side_by_side'" class="wb-side-grid">
          <div class="wb-pane">
            <div class="wb-pane-head tabular">
              <span>原稿底本</span>
              <span class="wb-pane-count">{{ currentDiff.stats.char_original }} 字</span>
            </div>
            <pre class="wb-pane-body">{{ currentDiff.original_text }}</pre>
          </div>
          <div class="wb-pane">
            <div class="wb-pane-head tabular">
              <span>文炼重构稿</span>
              <span class="wb-pane-count">{{ currentDiff.stats.char_refactored }} 字</span>
            </div>
            <pre class="wb-pane-body">{{ currentDiff.refactored_text }}</pre>
          </div>
        </div>

        <!-- 模式 3：内联精修编辑（沉浸式书卷校勘） -->
        <div v-else class="wb-edit-wrap">
          <textarea
            v-model="draftText"
            class="wb-editor"
            placeholder="在此直接校勘精修本章正文，保存后将原子写入重构稿并立即重跑单章结构性质检…"
          ></textarea>
          <div class="wb-edit-foot">
            <span v-if="liveEditStats" class="wb-edit-stat tabular">
              当前草稿 <b>{{ liveEditStats.charEdited }}</b> 字（相对原文
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

      <!-- 右栏：编辑审校诊断卡片、篇幅比例条、操作按钮与键帽图例 -->
      <aside class="wb-col wb-right">
        <template v-if="currentDiff">
          <div class="wb-card">
            <div class="wb-card-title">第 {{ currentDiff.chapter_num }} 章 · 篇幅与改动</div>
            <div class="wb-kv">
              <span>章节标题</span>
              <b class="wb-kv-serif">{{ currentDiff.title }}</b>
            </div>
            <div class="wb-kv tabular">
              <span>字数变化</span>
              <b class="mono">
                {{ currentDiff.stats.char_original }} → {{ currentDiff.stats.char_refactored }}（{{
                  fmtDelta(currentDiff.stats.char_delta)
                }}）
              </b>
            </div>
            <div class="wb-kv tabular">
              <span>篇幅比例 / 改动率</span>
              <b class="mono">
                {{ fmtPct(currentDiff.stats.length_ratio) }} /
                {{ fmtPct(currentDiff.stats.change_ratio) }}
              </b>
            </div>
            <div class="wb-ratio-wrap" title="篇幅比例健康参考区间：60% – 145%">
              <div class="wb-ratio-track">
                <div
                  class="wb-ratio-fill"
                  :class="lengthRatioHealthy ? 'ok' : 'warn'"
                  :style="{ width: lengthRatioWidth }"
                ></div>
              </div>
            </div>
            <div class="wb-kv tabular">
              <span>单章重写次数</span>
              <span class="wb-regen-badge mono">已重写 {{ currentDiff.regen_count }} / 3 次</span>
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
                <div v-if="iss.evidence" class="wb-qc-ev mono">{{ iss.evidence }}</div>
              </div>
            </div>
          </div>

          <div
            v-if="
              selectedAuditItem &&
              (selectedAuditItem.findings.length > 0 ||
                selectedAuditItem.search_matches.snippets.length > 0)
            "
            class="wb-card"
          >
            <div class="wb-card-title">设定巡检与检索命中</div>
            <div v-if="selectedAuditItem.findings.length" class="wb-qc-list">
              <div
                v-for="(fd, fIdx) in selectedAuditItem.findings"
                :key="fIdx"
                class="wb-qc-item"
                :class="fd.severity === 'bad' ? 'bad' : 'warn'"
              >
                <div class="wb-qc-head">
                  <span class="wb-qc-sev">{{ fd.severity === 'bad' ? '违规' : '提示' }}</span>
                  <span>{{ fd.message }}</span>
                </div>
                <div v-if="fd.snippet" class="wb-qc-ev">{{ fd.snippet }}</div>
              </div>
            </div>
            <div v-if="selectedAuditItem.search_matches.snippets.length" class="wb-qc-list">
              <div
                v-for="(sn, sIdx) in selectedAuditItem.search_matches.snippets"
                :key="sIdx"
                class="wb-search-hit"
              >
                命中 {{ sIdx + 1 }}：{{ sn }}
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

          <div class="wb-keycap-card" aria-label="键盘流审校速查">
            <span class="wb-keycap-item"><kbd>J</kbd><kbd>K</kbd> 切章</span>
            <span class="wb-keycap-item"><kbd>1</kbd><kbd>2</kbd><kbd>3</kbd> 视图</span>
            <span class="wb-keycap-item"><kbd>A</kbd> 审定</span>
            <span class="wb-keycap-item"><kbd>Ctrl+S</kbd> 存稿</span>
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
  gap: 14px;
}

.wb-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--tf-border);
}

.wb-title-group {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.wb-seal {
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
}

.wb-title-group h2 {
  margin: 0;
  font-family: var(--tf-font-serif);
  font-size: 16px;
  font-weight: 600;
  color: var(--tf-ink);
  letter-spacing: 0.02em;
}

.wb-live-badge {
  background: var(--tf-brand-soft);
  color: var(--tf-brand);
  border: 1px solid var(--tf-brand-border);
  border-radius: var(--tf-radius-pill);
  padding: 2px 11px;
  font-size: 12px;
  font-weight: 600;
}

.wb-sub-badge {
  border-radius: var(--tf-radius-pill);
  padding: 2px 10px;
  font-size: 12px;
  font-weight: 500;
}

.wb-sub-badge.ok {
  background: var(--tf-ok-bg);
  color: var(--tf-ok);
  border: 1px solid var(--tf-ok-border);
}

.wb-sub-badge.warn {
  background: var(--tf-warn-bg);
  color: var(--tf-warn);
  border: 1px solid var(--tf-warn-border);
}

.wb-header-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.wb-shortcut-legend {
  font-size: 11px;
  color: var(--tf-ink-muted);
  background: var(--tf-surface-2);
  border: 1px solid var(--tf-border);
  border-radius: var(--tf-radius-sm);
  padding: 4px 10px;
}

/* 三栏主体网格 */
.wb-grid {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr) 300px;
  gap: 14px;
  min-height: 480px;
  max-height: 640px;
}

.wb-col {
  border: 1px solid var(--tf-border);
  border-radius: var(--tf-radius-md);
  background: var(--tf-surface-2);
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}

.wb-center {
  background: var(--tf-surface);
  box-shadow: var(--tf-shadow-sm);
}

/* 左栏：检索 + 筛选 + 章节卡片 */
.wb-search-bar {
  display: flex;
  gap: 6px;
  padding: 8px;
  border-bottom: 1px solid var(--tf-border);
  background: var(--tf-surface);
}

.wb-search-field {
  flex: 1;
  min-width: 0;
  position: relative;
  display: flex;
  align-items: center;
}

.wb-search-icon {
  position: absolute;
  left: 8px;
  color: var(--tf-ink-muted);
  font-size: 13px;
  pointer-events: none;
}

.wb-search-input {
  width: 100%;
  min-width: 0;
  padding: 5px 24px 5px 24px;
  font-size: 12px;
  border: 1px solid var(--tf-border-strong);
  border-radius: var(--tf-radius-sm);
  background: var(--tf-surface-elevated);
  color: var(--tf-ink);
  transition: border-color 0.15s var(--tf-ease);
}

.wb-search-input:focus {
  border-color: var(--tf-brand);
  outline: none;
}

.wb-search-clear {
  position: absolute;
  right: 4px;
  width: 18px;
  height: 18px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--tf-ink-muted);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.wb-search-clear:hover {
  background: var(--tf-surface-hover);
  color: var(--tf-ink);
}

.wb-search-btn {
  padding: 5px 10px;
  font-size: 12px;
  font-weight: 500;
  border: 1px solid var(--tf-border-strong);
  border-radius: var(--tf-radius-sm);
  background: var(--tf-surface-2);
  color: var(--tf-ink-secondary);
  cursor: pointer;
  transition: background 0.15s var(--tf-ease);
}

.wb-search-btn:hover:not(:disabled) {
  background: var(--tf-surface-hover);
  color: var(--tf-ink);
}

.wb-audit-strip {
  padding: 6px 10px;
  font-size: 11.5px;
  color: var(--tf-warn);
  background: var(--tf-warn-bg);
  border-bottom: 1px solid var(--tf-warn-border);
  line-height: 1.45;
}

.wb-tabs {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 2px;
  padding: 4px 6px;
  border-bottom: 1px solid var(--tf-border);
  background: var(--tf-surface-2);
}

.wb-tab {
  border: none;
  border-radius: var(--tf-radius-xs);
  background: transparent;
  padding: 5px 2px;
  font-size: 11.5px;
  color: var(--tf-ink-muted);
  cursor: pointer;
  transition:
    background 0.14s var(--tf-ease),
    color 0.14s var(--tf-ease);
}

.wb-tab:hover {
  color: var(--tf-ink);
}

.wb-tab.active {
  background: var(--tf-surface);
  color: var(--tf-brand);
  font-weight: 600;
  box-shadow: var(--tf-shadow-sm);
}

.wb-chapter-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.wb-chapter-item {
  text-align: left;
  border: 1px solid var(--tf-border);
  border-left: 3px solid transparent;
  background: var(--tf-surface);
  border-radius: var(--tf-radius-sm);
  padding: 8px 10px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 5px;
  transition:
    border-color 0.15s var(--tf-ease),
    background 0.15s var(--tf-ease),
    box-shadow 0.15s var(--tf-ease);
}

.wb-chapter-item:hover {
  border-color: var(--tf-border-strong);
  background: var(--tf-surface-elevated);
}

.wb-chapter-item.selected {
  border-color: var(--tf-brand-border);
  border-left-color: var(--tf-brand);
  background: var(--tf-brand-soft);
  box-shadow: var(--tf-shadow-sm);
}

.wb-ch-top {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
}

.wb-ch-num {
  color: var(--tf-ink-muted);
  font-family: var(--tf-font-mono);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.wb-ch-title {
  flex: 1;
  font-family: var(--tf-font-serif);
  font-weight: 600;
  color: var(--tf-ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wb-qc-dot {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--tf-danger);
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.wb-audit-dot {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--tf-warn);
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.wb-ch-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
}

.wb-pill {
  padding: 1px 7px;
  border-radius: var(--tf-radius-pill);
  font-size: 11px;
  font-weight: 500;
}

.wb-pill.pending {
  background: var(--tf-surface-2);
  color: var(--tf-ink-muted);
}

.wb-pill.ready {
  background: var(--tf-brand-soft);
  color: var(--tf-brand);
}

.wb-pill.ok {
  background: var(--tf-ok-bg);
  color: var(--tf-ok);
}

.wb-pill.edited {
  background: var(--tf-edited-bg);
  color: var(--tf-edited);
}

.wb-chars {
  color: var(--tf-ink-muted);
  font-family: var(--tf-font-mono);
  font-size: 11px;
}

.wb-ch-snip {
  font-size: 11px;
  color: var(--tf-brand);
  background: var(--tf-brand-soft);
  border-radius: var(--tf-radius-xs);
  padding: 2px 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wb-ch-snip.warn {
  color: var(--tf-warn);
  background: var(--tf-warn-bg);
}

/* 中栏：文学书卷手稿区 */
.wb-mode-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 12px;
  border-bottom: 1px solid var(--tf-border);
  background: var(--tf-surface-2);
  gap: 8px;
}

.wb-modes {
  display: flex;
  gap: 6px;
}

.wb-mode-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 11px;
  font-size: 12px;
  font-weight: 500;
  border: 1px solid var(--tf-border-strong);
  background: var(--tf-surface);
  color: var(--tf-ink-secondary);
  border-radius: var(--tf-radius-sm);
  cursor: pointer;
  transition:
    background 0.15s var(--tf-ease),
    border-color 0.15s var(--tf-ease),
    color 0.15s var(--tf-ease);
}

.wb-mode-btn:hover {
  color: var(--tf-ink);
  background: var(--tf-surface-hover);
}

.wb-mode-btn.active {
  background: var(--tf-brand);
  border-color: var(--tf-brand);
  color: var(--tf-ink-on-brand);
}

.wb-mode-btn.active .wb-mode-key {
  background: rgba(255, 255, 255, 0.18);
  border-color: rgba(255, 255, 255, 0.35);
  color: var(--tf-ink-on-brand);
}

.wb-mode-key {
  min-width: 15px;
  padding: 0 4px;
  font-size: 10px;
}

.wb-notice {
  font-size: 12px;
  font-weight: 500;
  color: var(--tf-ok);
}

.wb-diff-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 22px 28px;
  background: var(--tf-surface);
  color: var(--tf-ink);
}

.wb-manuscript-head {
  padding-bottom: 12px;
  margin-bottom: 16px;
  border-bottom: 1px dashed var(--tf-border-strong);
}

.wb-ms-num {
  display: block;
  font-size: 10.5px;
  letter-spacing: 0.12em;
  color: var(--tf-ink-muted);
  margin-bottom: 4px;
}

.wb-ms-title {
  margin: 0;
  font-family: var(--tf-font-serif);
  font-size: 18px;
  font-weight: 600;
  color: var(--tf-ink);
  letter-spacing: 0.02em;
}

.wb-manuscript-body {
  font-family: var(--tf-font-serif);
  font-size: 15.5px;
  line-height: 1.88;
  letter-spacing: 0.012em;
  white-space: pre-wrap;
  word-break: break-word;
}

.wb-diff-block {
  display: inline;
}

.wb-seg-del {
  background: var(--tf-diff-del-bg);
  color: var(--tf-diff-del-ink);
  text-decoration: line-through;
  text-decoration-thickness: 1.2px;
  padding: 1px 3px;
  border-radius: 3px;
}

.wb-seg-ins {
  background: var(--tf-diff-ins-bg);
  color: var(--tf-diff-ins-ink);
  text-decoration: none;
  border-bottom: 1.5px solid var(--tf-diff-ins-line);
  padding: 1px 3px;
  border-radius: 3px;
}

.wb-seg-replace {
  display: inline;
}

.wb-side-grid {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  min-height: 0;
  background: var(--tf-surface);
}

.wb-pane {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-right: 1px solid var(--tf-border);
}

.wb-pane:last-child {
  border-right: none;
}

.wb-pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 14px;
  font-size: 12px;
  font-weight: 600;
  color: var(--tf-ink-secondary);
  background: var(--tf-surface-2);
  border-bottom: 1px solid var(--tf-border);
}

.wb-pane-count {
  font-family: var(--tf-font-mono);
  font-size: 11px;
  font-weight: 500;
  color: var(--tf-ink-muted);
}

.wb-pane-body {
  flex: 1;
  margin: 0;
  padding: 18px 20px;
  overflow-y: auto;
  font-family: var(--tf-font-serif);
  font-size: 15px;
  line-height: 1.85;
  letter-spacing: 0.01em;
  color: var(--tf-ink);
  white-space: pre-wrap;
  word-break: break-word;
}

.wb-edit-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 12px 14px;
  gap: 10px;
  background: var(--tf-surface);
}

.wb-editor {
  flex: 1;
  width: 100%;
  resize: none;
  padding: 18px 22px;
  border: 1px solid var(--tf-border-strong);
  border-radius: var(--tf-radius-md);
  background: var(--tf-surface-elevated);
  color: var(--tf-ink);
  font-family: var(--tf-font-serif);
  font-size: 15.5px;
  line-height: 1.88;
  letter-spacing: 0.012em;
  transition:
    border-color 0.15s var(--tf-ease),
    box-shadow 0.15s var(--tf-ease);
}

.wb-editor:focus {
  border-color: var(--tf-brand);
  box-shadow: 0 0 0 3px var(--tf-brand-soft);
  outline: none;
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
  color: var(--tf-ink-secondary);
}

.wb-dirty-flag {
  color: var(--tf-accent);
}

.wb-edit-actions {
  display: flex;
  gap: 8px;
}

/* 右栏：编辑审校诊断区 */
.wb-right {
  padding: 10px;
  gap: 10px;
  overflow-y: auto;
}

.wb-card {
  background: var(--tf-surface);
  border: 1px solid var(--tf-border);
  border-radius: var(--tf-radius-sm);
  padding: 11px 12px;
  display: flex;
  flex-direction: column;
  gap: 7px;
  box-shadow: var(--tf-shadow-sm);
}

.wb-card-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--tf-ink);
  border-bottom: 1px solid var(--tf-border);
  padding-bottom: 5px;
  letter-spacing: 0.02em;
}

.wb-kv {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--tf-ink-secondary);
}

.wb-kv b {
  color: var(--tf-ink);
  text-align: right;
}

.wb-kv-serif {
  font-family: var(--tf-font-serif);
}

.wb-ratio-wrap {
  padding: 2px 0;
}

.wb-ratio-track {
  height: 5px;
  background: var(--tf-surface-2);
  border-radius: var(--tf-radius-pill);
  overflow: hidden;
}

.wb-ratio-fill {
  height: 100%;
  border-radius: var(--tf-radius-pill);
  transition: width 0.28s var(--tf-ease);
}

.wb-ratio-fill.ok {
  background: var(--tf-ok);
}

.wb-ratio-fill.warn {
  background: var(--tf-warn);
}

.wb-regen-badge {
  background: var(--tf-surface-2);
  color: var(--tf-ink-secondary);
  border: 1px solid var(--tf-border);
  border-radius: var(--tf-radius-xs);
  padding: 1px 7px;
  font-size: 11px;
  font-weight: 600;
}

.wb-qc-card {
  flex: 1;
  min-height: 110px;
}

.wb-qc-clean {
  font-size: 12px;
  color: var(--tf-ok);
  background: var(--tf-ok-bg);
  border: 1px solid var(--tf-ok-border);
  border-radius: var(--tf-radius-sm);
  padding: 8px 10px;
  line-height: 1.5;
}

.wb-qc-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow-y: auto;
}

.wb-qc-item {
  border-radius: var(--tf-radius-sm);
  padding: 7px 9px;
  font-size: 12px;
  line-height: 1.5;
}

.wb-qc-item.bad {
  background: var(--tf-danger-bg);
  border: 1px solid var(--tf-danger-border);
  color: var(--tf-danger);
}

.wb-qc-item.warn {
  background: var(--tf-warn-bg);
  border: 1px solid var(--tf-warn-border);
  color: var(--tf-warn);
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
  opacity: 0.88;
  word-break: break-all;
}

.wb-search-hit {
  font-size: 11.5px;
  color: var(--tf-brand);
  background: var(--tf-brand-soft);
  border: 1px solid var(--tf-brand-border);
  border-radius: var(--tf-radius-sm);
  padding: 6px 8px;
  word-break: break-all;
  line-height: 1.5;
}

.wb-actions-stack {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.wb-btn {
  padding: 7px 12px;
  border-radius: var(--tf-radius-sm);
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid var(--tf-border-strong);
  background: var(--tf-surface);
  color: var(--tf-ink);
  transition:
    background 0.15s var(--tf-ease),
    border-color 0.15s var(--tf-ease),
    color 0.15s var(--tf-ease);
}

.wb-btn:hover:not(:disabled) {
  background: var(--tf-surface-hover);
}

.wb-btn.ghost {
  background: transparent;
  color: var(--tf-ink-secondary);
}

.wb-btn.ghost:hover:not(:disabled) {
  background: var(--tf-surface-hover);
  color: var(--tf-ink);
}

.wb-btn:disabled {
  opacity: 0.42;
  cursor: not-allowed;
}

.wb-btn.primary {
  background: var(--tf-brand);
  border-color: var(--tf-brand);
  color: var(--tf-ink-on-brand);
}

.wb-btn.primary:hover:not(:disabled) {
  background: var(--tf-brand-hover);
  border-color: var(--tf-brand-hover);
}

.wb-btn.approve {
  background: var(--tf-ok);
  border-color: var(--tf-ok);
  color: #fff;
  font-weight: 600;
}

.wb-btn.approve:hover:not(:disabled) {
  filter: brightness(0.94);
}

.wb-btn.regen {
  background: var(--tf-surface);
  border-color: var(--tf-accent-border);
  color: var(--tf-accent);
  font-weight: 600;
}

.wb-btn.regen:hover:not(:disabled) {
  background: var(--tf-accent-soft);
}

.wb-keycap-card {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 10px;
  padding: 8px 10px;
  border-radius: var(--tf-radius-sm);
  background: var(--tf-surface);
  border: 1px dashed var(--tf-border-strong);
  font-size: 11px;
  color: var(--tf-ink-muted);
}

.wb-keycap-item {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.wb-empty {
  padding: 22px 14px;
  text-align: center;
  font-size: 12.5px;
  color: var(--tf-ink-muted);
}

.wb-center-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--tf-surface);
}

@media (max-width: 1100px) {
  .wb-grid {
    grid-template-columns: 1fr;
    max-height: none;
  }
}
</style>
