import { apiFetch, postWithBusyRetry, type BusyRetryOptions } from './client';
import type { QualityIssue } from './system';

export type ReviewStatus = 'pending' | 'approved' | 'edited';
export type ChapterFilterTab = 'all' | 'pending' | 'qc' | 'audit' | 'approved';
export type WorkbenchCenterMode = 'diff' | 'side_by_side' | 'edit';
export type DiffOp = 'equal' | 'delete' | 'insert' | 'replace';
export type AuditFindingCategory =
  'forbidden_canon' | 'old_name' | 'length_compression' | 'length_bloat' | 'stitch_boundary';

export interface ChapterAuditFinding {
  severity: 'bad' | 'warn';
  category: AuditFindingCategory;
  message: string;
  snippet: string;
}

export interface ChapterSearchMatch {
  count: number;
  snippets: string[];
}

export interface ChapterAuditItem {
  chapter_num: number;
  chapter_id: string;
  title: string;
  char_raw: number;
  char_refactored: number;
  length_ratio: number | null;
  findings: ChapterAuditFinding[];
  search_matches: ChapterSearchMatch;
}

export interface ChapterAuditSummary {
  audited_chapters: number;
  flagged_chapters: number;
  forbidden_hits: number;
  old_name_hits: number;
  length_anomalies: number;
  stitch_warnings: number;
  search_matched_chapters: number;
  search_total_matches: number;
}

export interface ChapterAuditResponse {
  output_path: string;
  query: string;
  summary: ChapterAuditSummary;
  items: ChapterAuditItem[];
}

export interface WorkbenchFilterOptions {
  auditFlaggedNums?: ReadonlySet<number>;
  searchMatchedNums?: ReadonlySet<number> | null;
}

export interface WorkbenchShortcutInput {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  repeat?: boolean;
  targetTagName?: string | null;
  isContentEditable?: boolean;
  inEditMode?: boolean;
  collapsed?: boolean;
}

export type WorkbenchShortcutAction =
  | 'next_chapter'
  | 'prev_chapter'
  | 'toggle_approve'
  | 'mode_diff'
  | 'mode_side_by_side'
  | 'mode_edit'
  | 'save_edit';

export interface ChapterSummaryItem {
  chapter_num: number;
  chapter_id: string;
  title: string;
  status: string;
  review_status: ReviewStatus;
  qc_issues: QualityIssue[];
  regen_count: number;
  char_raw: number;
  char_refactored: number;
}

export interface ChapterListResponse {
  output_path: string;
  chapters: ChapterSummaryItem[];
  total: number;
  ready_count: number;
  approved_count: number;
  edited_count: number;
  flagged_count: number;
}

export interface DiffBlock {
  op: DiffOp;
  original_text: string;
  refactored_text: string;
  char_original: number;
  char_refactored: number;
}

export interface DiffStats {
  char_original: number;
  char_refactored: number;
  char_delta: number;
  length_ratio: number | null;
  change_ratio: number;
  equal_blocks: number;
  insert_blocks: number;
  delete_blocks: number;
  replace_blocks: number;
  equal_chars: number;
  inserted_chars: number;
  deleted_chars: number;
}

export interface ChapterDiffResponse {
  chapter_num: number;
  chapter_id: string;
  title: string;
  status: string;
  original_text: string;
  refactored_text: string;
  diff_blocks: DiffBlock[];
  stats: DiffStats;
  qc_issues: QualityIssue[];
  regen_count: number;
  review_status: ReviewStatus;
}

export interface ChapterReviewPayload {
  output_path?: string;
  run_id?: string | null;
  review_status?: ReviewStatus;
  edited_text?: string;
}

export interface ChapterReviewResponse {
  status: string;
  chapter_num: number;
  chapter_id: string;
  review_status: ReviewStatus;
  qc_issues: QualityIssue[];
  char_raw: number;
  char_refactored: number;
  diff_stats?: DiffStats;
}

export interface InlineDiffSummary {
  charOriginal: number;
  charEdited: number;
  charDelta: number;
  lengthRatio: number | null;
  changeRatio: number;
}

async function parseJsonOrThrow<T>(res: Response): Promise<T> {
  const text = await res.text();
  let body: any = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const detail = body?.detail ?? body ?? `HTTP ${res.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return body as T;
}

export async function fetchChapterList(
  port: number,
  outputPath?: string,
): Promise<ChapterListResponse> {
  const q = outputPath ? `?${new URLSearchParams({ output_path: outputPath }).toString()}` : '';
  const res = await apiFetch(`http://127.0.0.1:${port}/api/chapters${q}`);
  return parseJsonOrThrow<ChapterListResponse>(res);
}

export async function fetchChapterDiff(
  port: number,
  chapterNum: number,
  outputPath?: string,
): Promise<ChapterDiffResponse> {
  const q = outputPath ? `?${new URLSearchParams({ output_path: outputPath }).toString()}` : '';
  const res = await apiFetch(`http://127.0.0.1:${port}/api/chapters/${chapterNum}/diff${q}`);
  return parseJsonOrThrow<ChapterDiffResponse>(res);
}

export async function submitChapterReview(
  port: number,
  chapterNum: number,
  payload: ChapterReviewPayload,
  opts: BusyRetryOptions = {},
): Promise<ChapterReviewResponse> {
  const res = await postWithBusyRetry(
    `http://127.0.0.1:${port}/api/chapters/${chapterNum}/review`,
    payload,
    opts,
  );
  return parseJsonOrThrow<ChapterReviewResponse>(res);
}

export async function fetchChaptersAudit(
  port: number,
  outputPath?: string,
  query?: string,
): Promise<ChapterAuditResponse> {
  const params = new URLSearchParams();
  if (outputPath) params.set('output_path', outputPath);
  if (query && query.trim()) params.set('q', query.trim());
  const qs = params.toString();
  const res = await apiFetch(`http://127.0.0.1:${port}/api/chapters/audit${qs ? `?${qs}` : ''}`);
  return parseJsonOrThrow<ChapterAuditResponse>(res);
}

function splitSentencesCompact(text: string): string[] {
  const compact = (text || '').replace(/\s+/g, '');
  if (!compact) return [];
  return compact
    .split(/[。！？…；!?;]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * 浏览器端轻量内联精修统计（不替代服务端精确 SequenceMatcher，用于编辑态实时字数与改动率预览）。
 */
export function computeInlineDiffSummary(
  originalText: string,
  editedText: string,
): InlineDiffSummary {
  const origClean = (originalText || '').replace(/\s+/g, '');
  const editClean = (editedText || '').replace(/\s+/g, '');
  const charOriginal = origClean.length;
  const charEdited = editClean.length;
  const charDelta = charEdited - charOriginal;
  const lengthRatio =
    charOriginal > 0 ? Math.round((charEdited / charOriginal) * 1000) / 1000 : null;

  if (charOriginal === 0 && charEdited === 0) {
    return { charOriginal: 0, charEdited: 0, charDelta: 0, lengthRatio: null, changeRatio: 0 };
  }
  if (origClean === editClean) {
    return { charOriginal, charEdited, charDelta, lengthRatio, changeRatio: 0 };
  }

  const origSents = splitSentencesCompact(originalText);
  const editSents = splitSentencesCompact(editedText);
  if (!origSents.length) {
    return { charOriginal, charEdited, charDelta, lengthRatio, changeRatio: 1 };
  }

  const counts = new Map<string, number>();
  for (const s of editSents) {
    counts.set(s, (counts.get(s) || 0) + 1);
  }
  let kept = 0;
  for (const s of origSents) {
    const left = counts.get(s) || 0;
    if (left > 0) {
      kept += 1;
      counts.set(s, left - 1);
    }
  }
  const changeRatio = Math.max(
    0,
    Math.min(1, Math.round((1 - kept / origSents.length) * 1000) / 1000),
  );
  return {
    charOriginal,
    charEdited,
    charDelta,
    lengthRatio,
    changeRatio,
  };
}

export function filterChaptersForWorkbench(
  chapters: ChapterSummaryItem[],
  filter: ChapterFilterTab,
  options: WorkbenchFilterOptions = {},
): ChapterSummaryItem[] {
  let base: ChapterSummaryItem[];
  switch (filter) {
    case 'pending':
      base = chapters.filter(
        (c) => c.status === 'ready_for_review' && c.review_status === 'pending',
      );
      break;
    case 'qc':
      base = chapters.filter((c) => Array.isArray(c.qc_issues) && c.qc_issues.length > 0);
      break;
    case 'audit': {
      const flagged = options.auditFlaggedNums;
      base = flagged ? chapters.filter((c) => flagged.has(c.chapter_num)) : [];
      break;
    }
    case 'approved':
      base = chapters.filter((c) => c.review_status === 'approved' || c.review_status === 'edited');
      break;
    case 'all':
    default:
      base = chapters;
      break;
  }
  if (options.searchMatchedNums) {
    const matched = options.searchMatchedNums;
    return base.filter((c) => matched.has(c.chapter_num));
  }
  return base;
}

/**
 * 解析三栏工作台键盘流快捷键：
 * - 内联精修态下支持 `Ctrl+S` / `Cmd+S` 保存草稿并重跑单章质检（允许焦点在 textarea 内）；
 * - 其余单键（`j`/`k`/`a`/`1`/`2`/`3`）及 `Alt+↑`/`Alt+↓` 仅在焦点不在输入控件内时生效。
 */
export function resolveWorkbenchShortcut(
  input: WorkbenchShortcutInput,
): WorkbenchShortcutAction | null {
  if (input.collapsed || input.repeat) return null;

  const keyLower = (input.key || '').toLowerCase();
  const hasCmdOrCtrl = Boolean(input.ctrlKey || input.metaKey);

  if (hasCmdOrCtrl && !input.altKey && keyLower === 's') {
    return input.inEditMode ? 'save_edit' : null;
  }

  const tag = (input.targetTagName || '').toUpperCase();
  const isTyping =
    Boolean(input.isContentEditable) || tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
  if (isTyping) return null;

  if (input.altKey && !hasCmdOrCtrl && !input.shiftKey) {
    if (input.key === 'ArrowDown') return 'next_chapter';
    if (input.key === 'ArrowUp') return 'prev_chapter';
    return null;
  }

  if (hasCmdOrCtrl || input.altKey || input.shiftKey) return null;

  switch (keyLower) {
    case 'j':
      return 'next_chapter';
    case 'k':
      return 'prev_chapter';
    case 'a':
      return 'toggle_approve';
    case '1':
      return 'mode_diff';
    case '2':
      return 'mode_side_by_side';
    case '3':
      return 'mode_edit';
    default:
      return null;
  }
}

export function isReviewWorkbenchPhase(
  rawPhase: string,
  completed: boolean,
  readyCount: number,
): boolean {
  if (completed || readyCount > 0) return true;
  return (
    rawPhase === 'phase3' ||
    rawPhase === 'phase3-1' ||
    rawPhase === 'phase3_accept' ||
    rawPhase === 'phase3_qc' ||
    rawPhase === 'phase4'
  );
}
