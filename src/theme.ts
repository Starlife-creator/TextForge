/**
 * 现代东方书卷工坊主题（宣纸昼光 / 墨砚夜读）与五阶流水线步进状态纯函数。
 *
 * 主题持久化单一主键为 `localStorage['tf-theme']`（`'light' | 'dark'`），
 * 同时向后兼容旧版 `textforge_prefs_v1.darkMode` 布尔字段，确保老用户升级无感。
 */

export type ThemeMode = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'tf-theme';
export const LEGACY_PREFS_KEY = 'textforge_prefs_v1';

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface RootElementLike {
  dataset?: Record<string, string | undefined>;
  setAttribute?(name: string, value: string): void;
  classList?: {
    toggle(token: string, force?: boolean): boolean;
  };
}

/** 读取初始主题：优先读 `tf-theme`，未设置时回退读取 `textforge_prefs_v1.darkMode`。 */
export function resolveInitialTheme(storage?: StorageLike | null): ThemeMode {
  if (!storage) return 'light';
  try {
    const explicit = storage.getItem(THEME_STORAGE_KEY);
    if (explicit === 'dark' || explicit === 'light') {
      return explicit;
    }
    const legacyRaw = storage.getItem(LEGACY_PREFS_KEY);
    if (legacyRaw) {
      const parsed = JSON.parse(legacyRaw);
      if (typeof parsed?.darkMode === 'boolean') {
        return parsed.darkMode ? 'dark' : 'light';
      }
    }
  } catch {
    /* 忽略受限存储或损坏 JSON */
  }
  return 'light';
}

/** 将主题同步应用到根节点（同时写入 `data-theme` 属性与 `.dark` 类名）。 */
export function applyThemeToRoot(theme: ThemeMode, root?: RootElementLike | null): void {
  const el =
    root ??
    (typeof document !== 'undefined' ? (document.documentElement as RootElementLike) : null);
  if (!el) return;
  if (el.setAttribute) {
    el.setAttribute('data-theme', theme);
  } else if (el.dataset) {
    el.dataset.theme = theme;
  }
  el.classList?.toggle('dark', theme === 'dark');
}

/** 持久化用户显式选择的主题到 `tf-theme`。 */
export function persistThemeChoice(theme: ThemeMode, storage?: StorageLike | null): void {
  const target = storage ?? (typeof localStorage !== 'undefined' ? localStorage : null);
  if (!target) return;
  try {
    target.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    /* 忽略受限存储异常 */
  }
}

export type PipelineStepKey = 'split' | 'diagnose' | 'blueprint' | 'refactor' | 'final';
export type PipelineStepState = 'done' | 'active' | 'pending';

export interface PipelineStepItem {
  key: PipelineStepKey;
  label: string;
  state: PipelineStepState;
}

const PIPELINE_STEPS: { key: PipelineStepKey; label: string }[] = [
  { key: 'split', label: '拆书' },
  { key: 'diagnose', label: '诊断' },
  { key: 'blueprint', label: '蓝图' },
  { key: 'refactor', label: '重构缝合' },
  { key: 'final', label: '成书' },
];

function phaseToStepIndex(rawPhase: string, currentPhaseLabel: string): number {
  const p = rawPhase.trim();
  if (p === 'phase4' || currentPhaseLabel === '完成') return 4;
  if (p.startsWith('phase3')) return 3;
  if (p.startsWith('phase2') || currentPhaseLabel.startsWith('阶段2')) return 2;
  if (p.startsWith('phase1') || currentPhaseLabel.startsWith('阶段1')) return 1;
  if (
    p.startsWith('phase0') ||
    currentPhaseLabel.startsWith('阶段0') ||
    currentPhaseLabel === '启动'
  ) {
    return 0;
  }
  return -1;
}

/** 计算顶栏五阶书卷工序步进条状态（拆书 · 诊断 · 蓝图 · 重构缝合 · 成书）。 */
export function computePipelineSteps(opts: {
  rawPhase: string;
  currentPhaseLabel?: string;
  finished?: boolean;
  running?: boolean;
}): PipelineStepItem[] {
  const label = opts.currentPhaseLabel || '';
  if (opts.finished || label === '完成') {
    return PIPELINE_STEPS.map((s) => ({ ...s, state: 'done' }));
  }
  const activeIdx = phaseToStepIndex(opts.rawPhase || '', label);
  if (activeIdx < 0) {
    if (opts.running) {
      return PIPELINE_STEPS.map((s, idx) => ({
        ...s,
        state: idx === 0 ? 'active' : 'pending',
      }));
    }
    return PIPELINE_STEPS.map((s) => ({ ...s, state: 'pending' }));
  }
  return PIPELINE_STEPS.map((s, idx) => {
    if (idx < activeIdx) return { ...s, state: 'done' };
    if (idx === activeIdx) return { ...s, state: 'active' };
    return { ...s, state: 'pending' };
  });
}
