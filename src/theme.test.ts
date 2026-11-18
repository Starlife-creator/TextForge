/**
 * 东方书卷工坊双主题持久化、五阶工序步进条与设计令牌守卫单测（`src/theme.test.ts`）。
 */
import { describe, expect, it } from 'vitest';
import {
  applyThemeToRoot,
  computePipelineSteps,
  LEGACY_PREFS_KEY,
  persistThemeChoice,
  resolveInitialTheme,
  THEME_STORAGE_KEY,
  type RootElementLike,
  type StorageLike,
} from './theme';
import appRaw from './App.vue?raw';
import workbenchRaw from './components/ChapterReviewWorkbench.vue?raw';

const nodeFs = (
  globalThis as unknown as {
    process: {
      getBuiltinModule: (id: string) => { readFileSync: (u: URL, enc: string) => string };
    };
  }
).process.getBuiltinModule('node:fs');
const sharedCss = nodeFs.readFileSync(new URL('./styles/shared.css', import.meta.url), 'utf-8');

function makeMockStorage(initial: Record<string, string> = {}): StorageLike & {
  dump: () => Record<string, string>;
} {
  const data = { ...initial };
  return {
    getItem(key: string) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null;
    },
    setItem(key: string, value: string) {
      data[key] = String(value);
    },
    dump: () => ({ ...data }),
  };
}

function makeMockRoot() {
  const attrs: Record<string, string> = {};
  const classes = new Set<string>();
  const root: RootElementLike = {
    setAttribute(name: string, value: string) {
      attrs[name] = value;
    },
    classList: {
      toggle(token: string, force?: boolean) {
        const shouldHave = force === undefined ? !classes.has(token) : force;
        if (shouldHave) classes.add(token);
        else classes.delete(token);
        return shouldHave;
      },
    },
  };
  return { root, attrs, classes };
}

describe('宣纸 / 墨砚双主题解析、挂载与持久化', () => {
  it('优先采用 tf-theme 显式键值，未设置时兼容旧版 textforge_prefs_v1.darkMode', () => {
    expect(resolveInitialTheme(makeMockStorage())).toBe('light');
    expect(resolveInitialTheme(makeMockStorage({ [THEME_STORAGE_KEY]: 'dark' }))).toBe('dark');
    expect(
      resolveInitialTheme(
        makeMockStorage({
          [THEME_STORAGE_KEY]: 'light',
          [LEGACY_PREFS_KEY]: JSON.stringify({ darkMode: true }),
        }),
      ),
    ).toBe('light');
    expect(
      resolveInitialTheme(
        makeMockStorage({
          [LEGACY_PREFS_KEY]: JSON.stringify({ darkMode: true }),
        }),
      ),
    ).toBe('dark');
    expect(resolveInitialTheme(makeMockStorage({ [LEGACY_PREFS_KEY]: '{broken-json' }))).toBe(
      'light',
    );
  });

  it('applyThemeToRoot 同步写入 data-theme 属性与 html.dark 类名，persistThemeChoice 写入 tf-theme', () => {
    const { root, attrs, classes } = makeMockRoot();
    const store = makeMockStorage();

    applyThemeToRoot('dark', root);
    persistThemeChoice('dark', store);
    expect(attrs['data-theme']).toBe('dark');
    expect(classes.has('dark')).toBe(true);
    expect(store.dump()[THEME_STORAGE_KEY]).toBe('dark');

    applyThemeToRoot('light', root);
    persistThemeChoice('light', store);
    expect(attrs['data-theme']).toBe('light');
    expect(classes.has('dark')).toBe(false);
    expect(store.dump()[THEME_STORAGE_KEY]).toBe('light');
  });
});

describe('五阶书卷工序步进条状态映射（拆书 · 诊断 · 蓝图 · 重构缝合 · 成书）', () => {
  it('按当前阶段准确计算 done / active / pending 状态', () => {
    const idle = computePipelineSteps({ rawPhase: '' });
    expect(idle.map((s) => s.label)).toEqual(['拆书', '诊断', '蓝图', '重构缝合', '成书']);
    expect(idle.every((s) => s.state === 'pending')).toBe(true);

    const inBlueprint = computePipelineSteps({
      rawPhase: 'phase2_waiting',
      running: true,
    });
    expect(inBlueprint.map((s) => s.state)).toEqual([
      'done',
      'done',
      'active',
      'pending',
      'pending',
    ]);

    const inRefactor = computePipelineSteps({
      rawPhase: 'phase3-1',
      running: true,
    });
    expect(inRefactor.map((s) => s.state)).toEqual(['done', 'done', 'done', 'active', 'pending']);

    const doneAll = computePipelineSteps({
      rawPhase: 'phase4',
      finished: true,
    });
    expect(doneAll.every((s) => s.state === 'done')).toBe(true);
  });
});

describe('现代东方书卷工坊视觉令牌与组件接线守卫', () => {
  it('shared.css 定义宣纸昼光与墨砚夜读双套 --tf-* 令牌及衬线字体栈', () => {
    expect(sharedCss).toContain('--tf-bg: #f6f3ec');
    expect(sharedCss).toContain('[data-theme=');
    expect(sharedCss).toContain('--tf-bg: #131417');
    expect(sharedCss).toContain('--tf-brand: #264653');
    expect(sharedCss).toContain('--tf-accent: #b5493a');
    expect(sharedCss).toContain('--tf-font-serif:');
    expect(sharedCss).toContain('tabular-nums');
  });

  it('App.vue 完整导入全部 7 个子组件并接入五阶步进条与宣纸/墨砚切换', () => {
    for (const comp of [
      'SettingsModal',
      'GateModal',
      'HistoryModal',
      'VersionsModal',
      'RunStats',
      'QualityModal',
      'ChapterReviewWorkbench',
    ]) {
      expect(appRaw, `缺少 ${comp} 导入`).toContain(`import ${comp} from`);
    }
    expect(appRaw).toContain('☀ 宣纸');
    expect(appRaw).toContain('☾ 墨砚');
    expect(appRaw).toContain('pipeline-stepper');
    expect(appRaw).not.toContain('#2d3748');
  });

  it('ChapterReviewWorkbench 采用 --tf-font-serif 书卷排版并移除旧版硬编码深灰底色', () => {
    expect(workbenchRaw).toContain('var(--tf-font-serif)');
    expect(workbenchRaw).toContain('var(--tf-diff-ins-bg)');
    expect(workbenchRaw).toContain('var(--tf-diff-del-bg)');
    expect(workbenchRaw).not.toContain('#2d3748');
    expect(workbenchRaw).not.toContain('#1a202c');
  });
});
