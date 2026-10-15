/**
 * api/client.ts 的单元测试。
 *
 * 为什么必须有：后端在 release 下会对所有接口校验 X-TextForge-Token，
 * 而这个头是 21 处 fetch 调用点唯一的中转站 —— 漏注入就是全部接口 401，
 * 表现却只是「界面一片空白」，极难联想到鉴权。
 * vue-tsc 只能证明「能编译」，证明不了「行为对」。
 */
import { describe, expect, it, beforeEach, vi } from 'vitest';
import { TOKEN_HEADER, apiFetch, authHeaders, getAuthToken, setAuthToken } from './client';

vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn(async () => 'tok-from-shell'),
  isTauri: vi.fn(() => true),
}));

describe('api/client token 注入', () => {
  beforeEach(() => {
    setAuthToken(null);
    vi.restoreAllMocks();
  });

  it('未设置 token 时不发送鉴权头（开发/测试模式）', () => {
    expect(authHeaders()).toEqual({});
    expect(getAuthToken()).toBeNull();
  });

  it('设置 token 后带在请求头里', () => {
    setAuthToken('secret-123');
    expect(authHeaders()).toEqual({ [TOKEN_HEADER]: 'secret-123' });
    expect(getAuthToken()).toBe('secret-123');
  });

  it('空串/空值被当作「无 token」，不会发出空头', () => {
    // 空头会让后端走 compare_digest 比对失败，等同于把请求锁死。
    setAuthToken('');
    expect(getAuthToken()).toBeNull();
    expect(authHeaders()).toEqual({});
    setAuthToken(null);
    expect(getAuthToken()).toBeNull();
  });

  it('apiFetch 把 token 合入请求头', async () => {
    const spy = vi.fn(async () => new Response('{}'));
    vi.stubGlobal('fetch', spy);
    setAuthToken('secret-123');

    await apiFetch('http://127.0.0.1:1234/api/status');

    expect(spy).toHaveBeenCalledTimes(1);
    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>)[TOKEN_HEADER]).toBe('secret-123');
  });

  it('apiFetch 保留调用方自带的其它请求头', async () => {
    // SSE 依赖 Accept: text/event-stream，被覆盖就会走普通 JSON 解析而失败。
    const spy = vi.fn(async () => new Response('{}'));
    vi.stubGlobal('fetch', spy);
    setAuthToken('secret-123');

    await apiFetch('http://127.0.0.1:1234/api/events', {
      headers: { Accept: 'text/event-stream' },
    });

    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    const h = init.headers as Record<string, string>;
    expect(h.Accept).toBe('text/event-stream');
    expect(h[TOKEN_HEADER]).toBe('secret-123');
  });

  it('无 token 时请求依然能发出（开发模式）', async () => {
    const spy = vi.fn(async () => new Response('{}'));
    vi.stubGlobal('fetch', spy);

    await apiFetch('http://127.0.0.1:1234/api/status');

    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.headers).toEqual({});
  });

  it('不修改 init 中已有的其它字段（method/body 等）', async () => {
    const spy = vi.fn(async () => new Response('{}'));
    vi.stubGlobal('fetch', spy);

    await apiFetch('http://127.0.0.1:1234/api/stop', {
      method: 'POST',
      body: '{}',
    });

    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.method).toBe('POST');
    expect(init.body).toBe('{}');
  });
});
