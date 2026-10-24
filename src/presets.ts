/** 上下文窗口与服务商预设的取值（纯数据，便于在不挂载组件的前提下做断言）。 */

/** 后端 `StartRequest.context_window` 的下限（models/start_request.py 的 ge=8000）。 */
export const MIN_CONTEXT_WINDOW = 8000;

/**
 * 默认窗口 32768（旧值 8000）。
 *
 * 依据是按 `core/estimate.py` 的算式推算的（不是真机实测）：24/60/200 章三种书里，
 * 8000 → 每批 2 章、脚手架占输入 68%；32768 → 每批 8 章（撞到单批 8 章上限）、占 40%；
 * 再往上（65536/131072）批数已经降不动了，但蓝图/前情/衔接的预留都随窗口放大，
 * 总 token 反而回升 15%~35%。所以这里刻意**不提供** 128k 档预设。
 * 判据本身由 `backend/tests/test_estimate.py` 的拐点用例守着。
 */
export const DEFAULT_CONTEXT_WINDOW = 32768;

export interface Preset {
  /** [诊断, 蓝图, 重构, 缝合] 四档温度 */
  temps: [number, number, number, number];
  ctx: number;
  interval: number;
}

/** B2 服务商预设：一键套用温度/上下文/批次间隔（纯前端，不写后端）。 */
export const PRESETS: Record<string, Preset> = {
  通用: { temps: [0.3, 0.5, 0.8, 0.6], ctx: DEFAULT_CONTEXT_WINDOW, interval: 2.0 },
  快速低保真: { temps: [0.2, 0.3, 0.7, 0.5], ctx: MIN_CONTEXT_WINDOW, interval: 1.0 },
  '创作·宽松': { temps: [0.5, 0.7, 1.0, 0.8], ctx: DEFAULT_CONTEXT_WINDOW, interval: 2.5 },
};

export const DEFAULT_PRESET_NAME = '通用';
