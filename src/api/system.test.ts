import { afterEach, describe, expect, it, vi } from 'vitest';
import { costOf, estimateRun, fetchQualityReport, formatCost } from './system';

describe('api/system 质量报告', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('把 output_path 作为查询参数编码，而不是拼进路径', async () => {
    const spy = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify({ status: 'good', summary: { chapters: 2 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', spy);

    const report = await fetchQualityReport(1234, 'E:\\书 名\\out');
    expect(report.status).toBe('good');

    const url = new URL(spy.mock.calls[0][0] as string);
    expect(url.origin).toBe('http://127.0.0.1:1234');
    expect(url.pathname).toBe('/api/output/quality');
    expect(url.searchParams.get('output_path')).toBe('E:\\书 名\\out');
  });

  it('后端 404 时把 detail 作为错误抛出（界面显示「尚无报告」）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: '尚无质量报告（完成一次重构后生成）' }), {
            status: 404,
            headers: { 'Content-Type': 'application/json' },
          }),
      ),
    );

    await expect(fetchQualityReport(1234, 'out')).rejects.toThrow('尚无质量报告');
  });
});

describe('单价折算', () => {
  it('按「元/百万 token」折算输入与输出', () => {
    expect(costOf(1_000_000, 0, { input: 2, output: 6 })).toBeCloseTo(2, 6);
    expect(costOf(0, 1_000_000, { input: 2, output: 6 })).toBeCloseTo(6, 6);
    expect(costOf(1_500_000, 500_000, { input: 2, output: 6 })).toBeCloseTo(6, 6);
  });

  it('后端 usage 给的是字符串也照算，负数按 0 处理', () => {
    expect(costOf('1000000', '500000', { input: 2, output: 6 })).toBeCloseTo(5, 6);
    expect(costOf(-5, 1_000_000, { input: 2, output: 6 })).toBeCloseTo(6, 6);
  });

  it('反向断言：单价没填时返回 null，而不是伪装成 ¥0.00', () => {
    expect(costOf(1_000_000, 1_000_000, { input: 0, output: 0 })).toBeNull();
    expect(costOf(1_000_000, 1_000_000, { input: NaN, output: NaN })).toBeNull();
  });

  it('只填一侧单价时另一侧按 0 计', () => {
    expect(costOf(1_000_000, 1_000_000, { input: 3, output: 0 })).toBeCloseTo(3, 6);
  });

  it('格式化：null 说明原因，小额给四位小数避免显示成 ¥0.00', () => {
    expect(formatCost(null)).toBe('本地模型（未计单价）');
    expect(formatCost(0.005)).toBe('¥0.0050');
    expect(formatCost(12.345)).toBe('¥12.35');
  });
});

describe('api/system 开跑前预估', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('POST 同一份 start 请求体并原样返回预估结果', async () => {
    const payload = { input_path: 'E:\书\a.txt', context_window: 32768 };
    const spy = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify({ batches: 3, chapters: 12 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', spy);

    const res = await estimateRun(1234, payload);
    expect(res.batches).toBe(3);
    expect(new URL(spy.mock.calls[0][0] as string).pathname).toBe('/api/estimate');
    expect(JSON.parse(String(spy.mock.calls[0][1]!.body))).toEqual(payload);
  });

  it('路径不合法时把后端 detail 抛给界面（与 /api/start 同一套校验）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: '输入格式选择(txt)与文件扩展名(.docx)不一致' }), {
            status: 422,
            headers: { 'Content-Type': 'application/json' },
          }),
      ),
    );
    await expect(estimateRun(1234, {})).rejects.toThrow('不一致');
  });
});
