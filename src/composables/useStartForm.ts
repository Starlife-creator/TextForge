/**
 * #11 开跑表单 composable（骨架，尚未接入 App.vue）。
 *
 * 迁移口径：`src/App.vue` 内联表单状态与校验仍是现行实现；本文件把表单模型、
 * 默认值与校验规则抽成可测单元，字段与后端 `StartRequest` 对齐，待 npm 环境下
 * 分批替换。未接入前不要在组件里调用本文件。
 *
 * 本轮缺 npm 构建/组件测试环境，未执行 `npm run build` / `vitest`（#11 ⬜）。
 */

import { reactive, ref } from 'vue';

export interface StartFormState {
  input_mode: 'single_file' | 'multi_file';
  input_path: string;
  output_path: string;
  input_format: 'docx' | 'txt' | 'md';
  api_url: string;
  model: string;
  context_window: number;
  author_style: string;
  refactor_mode: 'full_rewrite' | 'fidelity' | 'fix_gaps' | 'reskin';
  rich_text: boolean;
  export_epub: boolean;
  compare_output: boolean;
  chapter_snapshots: boolean;
  review_split: boolean;
  max_concurrency: number;
  batch_interval_sec: number;
  refactor_gates: Record<string, boolean>;
  forbidden_canon: string[];
  name_map: Record<string, string>;
  fix_list: string[];
}

function defaults(): StartFormState {
  return {
    input_mode: 'single_file',
    input_path: '',
    output_path: '',
    input_format: 'txt',
    api_url: '',
    model: '',
    context_window: 32768,
    author_style: '出版级文学重构，文笔凝练，注重画面感',
    refactor_mode: 'full_rewrite',
    rich_text: false,
    export_epub: false,
    compare_output: false,
    chapter_snapshots: false,
    review_split: false,
    max_concurrency: 1,
    batch_interval_sec: 2.0,
    refactor_gates: {
      lock_names: false,
      lock_plot: false,
      inject_forbidden: false,
      require_chapter_accept: false,
      skip_stitch_if_smooth: false,
      qc_block_export: false,
      auto_rewrite_bad_batch: true,
      stitch_minimal_change: true,
    },
    forbidden_canon: [],
    name_map: {},
    fix_list: [],
  };
}

export function useStartForm() {
  const form = reactive<StartFormState>(defaults());
  const errors = ref<string[]>([]);

  /** 校验通过返回 true；错误为 i18n 键位（见 src/i18n/zh-CN.ts）。 */
  function validate(): boolean {
    errors.value = [];
    if (!form.input_path.trim()) errors.value.push('form.inputPathRequired');
    if (!form.output_path.trim()) errors.value.push('form.outputPathRequired');
    if (!form.model.trim()) errors.value.push('form.modelRequired');
    if (!form.api_url.trim()) errors.value.push('form.apiUrlRequired');
    return errors.value.length === 0;
  }

  function reset(): void {
    Object.assign(form, defaults());
    errors.value = [];
  }

  return { form, errors, validate, reset };
}
