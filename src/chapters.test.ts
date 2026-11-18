/**
 * 边跑边审 API 封装、内联 Diff 统计与三栏工作台接线单测（`src/chapters.test.ts`）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { setAuthToken, TOKEN_HEADER } from './api/client';
import {
  computeInlineDiffSummary,
  fetchChapterDiff,
  fetchChapterList,
  fetchChaptersAudit,
  filterChaptersForWorkbench,
  isReviewWorkbenchPhase,
  resolveWorkbenchShortcut,
  submitChapterReview,
  type ChapterSummaryItem,
} from './api/chapters';
import appRaw from './App.vue?raw';
import workbenchRaw from './components/ChapterReviewWorkbench.vue?raw';

afterEach(() => {
  setAuthToken(null);
  vi.restoreAllMocks();
});

const SAMPLE_CHAPTERS: ChapterSummaryItem[] = [
  {
    chapter_num: 1,
    chapter_id: '1',
    title: '第一章 雨夜',
    status: 'ready_for_review',
    review_status: 'approved',
    qc_issues: [],
    regen_count: 0,
    char_raw: 1200,
    char_refactored: 1260,
  },
  {
    chapter_num: 2,
    chapter_id: '2',
    title: '第二章 惊变',
    status: 'ready_for_review',
    review_status: 'pending',
    qc_issues: [
      {
        severity: 'bad',
        code: 'placeholder',
        chapter_id: '2',
        message: '疑似占位',
        evidence: 'TODO',
      },
    ],
    regen_count: 1,
    char_raw: 1500,
    char_refactored: 320,
  },
  {
    chapter_num: 3,
    chapter_id: '3',
    title: '第三章 远行',
    status: 'pending',
    review_status: 'pending',
    qc_issues: [],
    regen_count: 0,
    char_raw: 1400,
    char_refactored: 0,
  },
];

describe('chapters API 客户端与鉴权/503 重试', () => {
  it('fetchChapterList 与 fetchChapterDiff 自动携带 X-TextForge-Token 并编码路径', async () => {
    setAuthToken('tok-workbench-1');
    const calls: { url: string; headers: Record<string, string> }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string, init?: RequestInit) => {
        calls.push({
          url: String(input),
          headers: (init?.headers as Record<string, string>) || {},
        });
        if (String(input).includes('/diff')) {
          return new Response(
            JSON.stringify({
              chapter_num: 1,
              chapter_id: '1',
              title: '第一章',
              status: 'ready_for_review',
              original_text: '原文。',
              refactored_text: '重构。',
              diff_blocks: [
                {
                  op: 'replace',
                  original_text: '原文。',
                  refactored_text: '重构。',
                  char_original: 3,
                  char_refactored: 3,
                },
              ],
              stats: {
                char_original: 3,
                char_refactored: 3,
                char_delta: 0,
                length_ratio: 1,
                change_ratio: 1,
                equal_blocks: 0,
                insert_blocks: 0,
                delete_blocks: 0,
                replace_blocks: 1,
                equal_chars: 0,
                inserted_chars: 3,
                deleted_chars: 3,
              },
              qc_issues: [],
              regen_count: 0,
              review_status: 'pending',
            }),
            { status: 200 },
          );
        }
        return new Response(
          JSON.stringify({
            output_path: 'E:/out/book',
            chapters: SAMPLE_CHAPTERS,
            total: 3,
            ready_count: 2,
            approved_count: 1,
            edited_count: 0,
            flagged_count: 1,
          }),
          { status: 200 },
        );
      }),
    );

    const list = await fetchChapterList(8123, 'E:\\out\\book 1');
    expect(list.total).toBe(3);
    expect(calls[0].url).toContain('/api/chapters?output_path=');
    expect(calls[0].headers[TOKEN_HEADER]).toBe('tok-workbench-1');

    const diff = await fetchChapterDiff(8123, 1, 'E:\\out\\book 1');
    expect(diff.diff_blocks[0].op).toBe('replace');
    expect(calls[1].url).toContain('/api/chapters/1/diff?output_path=');
    expect(calls[1].headers[TOKEN_HEADER]).toBe('tok-workbench-1');
  });

  it('submitChapterReview 遇到 503 文件被占用时自动走 postWithBusyRetry 重试', async () => {
    let n = 0;
    const busyEvents: number[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        n += 1;
        if (n === 1) {
          return new Response(JSON.stringify({ detail: '文件正被占用' }), { status: 503 });
        }
        return new Response(
          JSON.stringify({
            status: 'updated',
            chapter_num: 2,
            chapter_id: '2',
            review_status: 'edited',
            qc_issues: [],
            char_raw: 1500,
            char_refactored: 1520,
          }),
          { status: 200 },
        );
      }),
    );

    const res = await submitChapterReview(
      8123,
      2,
      { output_path: 'E:/out/book', edited_text: '人工精修后的完整正文。' },
      { delayMs: 5, onBusy: (attempt) => busyEvents.push(attempt) },
    );
    expect(n).toBe(2);
    expect(busyEvents).toEqual([2]);
    expect(res.review_status).toBe('edited');
  });
});

describe('工作台纯函数逻辑：内联 Diff 统计、章节筛选与激活阶段判定', () => {
  it('computeInlineDiffSummary 正确计算字数差、比例与句级改动率', () => {
    const identical = computeInlineDiffSummary('第一句。第二句。', '第一句。\n第二句。');
    expect(identical.changeRatio).toBe(0);
    expect(identical.charDelta).toBe(0);

    const edited = computeInlineDiffSummary(
      '第一句。第二句。',
      '第一句。改写后的第二句！新增第三句。',
    );
    expect(edited.charDelta).toBeGreaterThan(0);
    expect(edited.changeRatio).toBe(0.5);
  });

  it('filterChaptersForWorkbench 覆盖五个筛选标签与检索交集过滤', () => {
    expect(filterChaptersForWorkbench(SAMPLE_CHAPTERS, 'all')).toHaveLength(3);
    expect(
      filterChaptersForWorkbench(SAMPLE_CHAPTERS, 'pending').map((c) => c.chapter_num),
    ).toEqual([2]);
    expect(filterChaptersForWorkbench(SAMPLE_CHAPTERS, 'qc').map((c) => c.chapter_num)).toEqual([
      2,
    ]);
    expect(
      filterChaptersForWorkbench(SAMPLE_CHAPTERS, 'audit', {
        auditFlaggedNums: new Set([1, 3]),
      }).map((c) => c.chapter_num),
    ).toEqual([1, 3]);
    expect(
      filterChaptersForWorkbench(SAMPLE_CHAPTERS, 'all', {
        searchMatchedNums: new Set([2]),
      }).map((c) => c.chapter_num),
    ).toEqual([2]);
    expect(
      filterChaptersForWorkbench(SAMPLE_CHAPTERS, 'approved').map((c) => c.chapter_num),
    ).toEqual([1]);
  });

  it('fetchChaptersAudit 自动携带鉴权头并编码检索词 q', async () => {
    setAuthToken('tok-audit-9');
    let capturedUrl = '';
    let capturedToken = '';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string, init?: RequestInit) => {
        capturedUrl = String(input);
        capturedToken = ((init?.headers as Record<string, string>) || {})[TOKEN_HEADER] || '';
        return new Response(
          JSON.stringify({
            output_path: 'E:/out/book',
            query: '林渊',
            summary: {
              audited_chapters: 3,
              flagged_chapters: 1,
              forbidden_hits: 0,
              old_name_hits: 1,
              length_anomalies: 0,
              stitch_warnings: 0,
              search_matched_chapters: 1,
              search_total_matches: 2,
            },
            items: [],
          }),
          { status: 200 },
        );
      }),
    );

    const res = await fetchChaptersAudit(8123, 'E:\\out\\book', '  林渊  ');
    expect(res.query).toBe('林渊');
    expect(capturedToken).toBe('tok-audit-9');
    expect(capturedUrl).toContain('/api/chapters/audit?');
    expect(capturedUrl).toContain('q=%E6%9E%97%E6%B8%8A');
  });

  it('resolveWorkbenchShortcut 支持键盘流操作并在输入框聚焦时严格屏蔽单键误触', () => {
    // 非输入焦点下响应 j/k/Alt+方向键/a/1/2/3
    expect(resolveWorkbenchShortcut({ key: 'j', targetTagName: 'DIV' })).toBe('next_chapter');
    expect(resolveWorkbenchShortcut({ key: 'ArrowDown', altKey: true, targetTagName: 'DIV' })).toBe(
      'next_chapter',
    );
    expect(resolveWorkbenchShortcut({ key: 'k', targetTagName: 'BUTTON' })).toBe('prev_chapter');
    expect(resolveWorkbenchShortcut({ key: 'ArrowUp', altKey: true, targetTagName: 'DIV' })).toBe(
      'prev_chapter',
    );
    expect(resolveWorkbenchShortcut({ key: 'a', targetTagName: 'DIV' })).toBe('toggle_approve');
    expect(resolveWorkbenchShortcut({ key: '1', targetTagName: 'DIV' })).toBe('mode_diff');
    expect(resolveWorkbenchShortcut({ key: '2', targetTagName: 'DIV' })).toBe('mode_side_by_side');
    expect(resolveWorkbenchShortcut({ key: '3', targetTagName: 'DIV' })).toBe('mode_edit');

    // 反向安全守卫：在 INPUT / TEXTAREA 中敲击 j/k/a/1/2/3 绝不能劫持输入
    for (const k of ['j', 'k', 'a', '1', '2', '3']) {
      expect(
        resolveWorkbenchShortcut({ key: k, targetTagName: 'TEXTAREA', inEditMode: true }),
      ).toBe(null);
      expect(resolveWorkbenchShortcut({ key: k, targetTagName: 'INPUT' })).toBe(null);
    }

    // 内联精修模式下 Ctrl+S / Cmd+S 允许在 TEXTAREA 内保存，非精修模式不响应
    expect(
      resolveWorkbenchShortcut({
        key: 's',
        ctrlKey: true,
        targetTagName: 'TEXTAREA',
        inEditMode: true,
      }),
    ).toBe('save_edit');
    expect(
      resolveWorkbenchShortcut({
        key: 'S',
        metaKey: true,
        targetTagName: 'TEXTAREA',
        inEditMode: true,
      }),
    ).toBe('save_edit');
    expect(
      resolveWorkbenchShortcut({
        key: 's',
        ctrlKey: true,
        targetTagName: 'DIV',
        inEditMode: false,
      }),
    ).toBe(null);
    expect(resolveWorkbenchShortcut({ key: 'j', targetTagName: 'DIV', collapsed: true })).toBe(
      null,
    );
  });

  it('isReviewWorkbenchPhase 在 phase3 / phase3_accept / phase4 或已完成时激活', () => {
    expect(isReviewWorkbenchPhase('phase1', false, 0)).toBe(false);
    expect(isReviewWorkbenchPhase('phase3-1', false, 0)).toBe(true);
    expect(isReviewWorkbenchPhase('phase3_accept', false, 0)).toBe(true);
    expect(isReviewWorkbenchPhase('phase4', false, 0)).toBe(true);
    expect(isReviewWorkbenchPhase('phase0', true, 0)).toBe(true);
  });
});

describe('三栏审阅工作台与 App.vue 结构接线守卫', () => {
  it('App.vue 接入 batch_stream_ready 事件、review_summary 状态汇总与 ChapterReviewWorkbench 组件', () => {
    expect(appRaw).toContain("case 'batch_stream_ready'");
    expect(appRaw).toContain('<ChapterReviewWorkbench');
    expect(appRaw).toContain('showReviewWorkbench');
    expect(appRaw).toContain('review_summary');
    expect(appRaw).toContain('@review-updated="fetchStatus"');
  });

  it('ChapterReviewWorkbench 包含三栏结构、设定与篇幅巡检检索、键盘流快捷键与通过/重写入口', () => {
    expect(workbenchRaw).toContain('边跑边审：已完成');
    expect(workbenchRaw).toContain('🔍 设定与篇幅巡检');
    expect(workbenchRaw).toContain('快捷键：J/K 上下章');
    expect(workbenchRaw).toContain('fetchChaptersAudit');
    expect(workbenchRaw).toContain('resolveWorkbenchShortcut');
    expect(workbenchRaw).toContain('句级对照 Diff');
    expect(workbenchRaw).toContain('双栏并排');
    expect(workbenchRaw).toContain('内联精修编辑');
    expect(workbenchRaw).toContain('✓ 通过本章');
    expect(workbenchRaw).toContain('↻ 重写本章');
  });
});
