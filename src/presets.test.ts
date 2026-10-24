import { describe, expect, it } from 'vitest';
import {
  DEFAULT_CONTEXT_WINDOW,
  DEFAULT_PRESET_NAME,
  MIN_CONTEXT_WINDOW,
  PRESETS,
} from './presets';

describe('上下文窗口默认值与预设档位', () => {
  it('默认值就是「通用」预设的窗口，两处不得各写一个数', () => {
    expect(DEFAULT_CONTEXT_WINDOW).toBe(32768);
    expect(PRESETS[DEFAULT_PRESET_NAME].ctx).toBe(DEFAULT_CONTEXT_WINDOW);
  });

  it('本地小模型档位仍是最小窗口', () => {
    expect(PRESETS['快速低保真'].ctx).toBe(MIN_CONTEXT_WINDOW);
  });

  it('所有预设都落在后端 StartRequest 的合法区间内', () => {
    // 后端 models/start_request.py 是 context_window: Field(ge=8000)
    for (const [name, p] of Object.entries(PRESETS)) {
      expect(p.ctx, name).toBeGreaterThanOrEqual(MIN_CONTEXT_WINDOW);
      expect(p.ctx, name).toBeLessThanOrEqual(65536);
    }
  });

  it('刻意不提供 128k 档预设', () => {
    // 依据 backend/tests/test_estimate.py 的拐点用例：批数在 8 章/批触底后，
    // 继续放大窗口只会让蓝图/前情/衔接的预留跟着涨，总 token 反而回升。
    const maxCtx = Math.max(...Object.values(PRESETS).map((p) => p.ctx));
    expect(maxCtx).toBeLessThan(131072);
  });

  it('温度四档齐全且在可填范围内', () => {
    for (const [name, p] of Object.entries(PRESETS)) {
      expect(p.temps, name).toHaveLength(4);
      for (const t of p.temps) {
        expect(t, name).toBeGreaterThanOrEqual(0);
        expect(t, name).toBeLessThanOrEqual(2);
      }
      expect(p.interval, name).toBeGreaterThanOrEqual(0);
    }
  });
});
