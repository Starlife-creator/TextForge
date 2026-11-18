import { apiFetch, postWithBusyRetry, type BusyRetryOptions } from './client';
import type { QualityIssue } from './system';

export type ReviewStatus = 'pending' | 'approved' | 'edited';
export type ChapterFilterTab = 'all' | 'pending' | 'qc' | 'approved';
export type WorkbenchCenterMode = 'diff' | 'side_by_side' | 'edit';
export type DiffOp = 'equal' | 'delete' | 'insert' | 'replace';

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
): ChapterSummaryItem[] {
  switch (filter) {
    case 'pending':
      return chapters.filter(
        (c) => c.status === 'ready_for_review' && c.review_status === 'pending',
      );
    case 'qc':
      return chapters.filter((c) => Array.isArray(c.qc_issues) && c.qc_issues.length > 0);
    case 'approved':
      return chapters.filter((c) => c.review_status === 'approved' || c.review_status === 'edited');
    case 'all':
    default:
      return chapters;
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
