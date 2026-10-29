/**
 * App.vue 接线守卫（R10 / B3.2）。
 *
 * 为什么是「读源码断言」而不是渲染组件：本仓的 vitest 没有组件测试环境
 * （devDependencies 里没有 @vue/test-utils，也没有 happy-dom/jsdom），加依赖
 * 需要先过一轮决策。R10 这一类缺陷的共同点是**静默**——后端早就把
 * `gate_timeout`、`progress_error` 发出来了，前端没接上而已。所以这里至少保证：
 * 接没接上是一条会红的判据，而不是靠人肉点开界面才发现。
 */
import { describe, expect, it } from 'vitest';
import app from './App.vue?raw';

describe('SSE 事件与状态字段的接线', () => {
  it('闸门超时与 AI 拆分启动都有分支，未知事件不再静默丢弃', () => {
    expect(app).toContain("case 'gate_timeout'");
    expect(app).toContain("case 'split_ai_start'");
    expect(app).toContain('default:');
    expect(app).toContain('未处理的事件');
  });

  it('fetchStatus 消费 progress_error 与 gate_timed_out', () => {
    expect(app).toContain('s.progress_error');
    expect(app).toContain('s.gate_timed_out');
    expect(app).toContain('s.gate_timeout_gate');
  });

  it('人工闸门的确认请求走 503 自动重试，而不是裸 apiFetch', () => {
    for (const path of [
      '/api/split/confirm',
      '/api/fix/confirm',
      '/api/accept',
      '/api/blueprint/confirm',
      '/api/regen_chapter',
    ]) {
      const url = '`${apiBase()}' + path + '`';
      const at = app.indexOf(url);
      expect(at, `找不到对 ${path} 的调用`).toBeGreaterThan(-1);
      // 调用点前面 80 个字符里必须是 postWithBusyRetry，不能退回 apiFetch
      const before = app.slice(Math.max(0, at - 80), at);
      expect(before, `${path} 必须走 postWithBusyRetry`).toContain('postWithBusyRetry');
      expect(before).not.toContain('apiFetch');
    }
  });

  it('503 给的是「下一步做什么」而不是一句失败', () => {
    expect(app).toContain('正被占用');
    expect(app).toContain('自动重试');
  });

  it('重写次数没登记上时必须告诉用户（R12 的可见面）', () => {
    expect(app).toContain('regen_count_recorded');
  });
});
