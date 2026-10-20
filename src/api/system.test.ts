import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchQualityReport } from './system';

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
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: '尚无质量报告（完成一次重构后生成）' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );

    await expect(fetchQualityReport(1234, 'out')).rejects.toThrow('尚无质量报告');
  });
});
