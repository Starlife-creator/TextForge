/**
 * #11 中文文案表（集中管理，预留 locale 切换）。
 *
 * 迁移口径：当前 `src/App.vue` 内仍有大量硬编码中文字面量；本文件先落地键位骨架，
 * 待 npm 环境下分批把组件文案替换为 `t(key)` 调用。新增文案先登记到本表，
 * 不再在组件里新写裸中文字面量。
 *
 * 注意：本文件未接入任何组件，本轮不参与构建/测试（#11 ⬜：缺 npm 构建环境）。
 */

export type MessageKey =
  // 通用按钮
  | 'button.confirm'
  | 'button.reject'
  | 'button.pause'
  | 'button.resume'
  | 'button.stop'
  | 'button.estimate'
  | 'button.start'
  | 'button.exportDiagnostics'
  | 'button.qualityReport'
  | 'button.history'
  | 'button.versions'
  | 'button.comparison'
  // 闸门标题
  | 'gate.split.title'
  | 'gate.fixReview.title'
  | 'gate.blueprint.title'
  | 'gate.accept.title'
  | 'gate.qc.title'
  // 闸门提示
  | 'gate.split.hint'
  | 'gate.blueprint.hint'
  // 状态与阶段
  | 'status.idle'
  | 'status.running'
  | 'status.completed'
  | 'status.paused'
  | 'status.error'
  | 'phase.phase0'
  | 'phase.phase1'
  | 'phase.phase2'
  | 'phase.phase3'
  | 'phase.phase4'
  // 保护门标签
  | 'gate.lockNames'
  | 'gate.lockPlot'
  | 'gate.injectForbidden'
  | 'gate.requireChapterAccept'
  | 'gate.skipStitchIfSmooth'
  | 'gate.qcBlockExport'
  | 'gate.autoRewriteBadBatch'
  | 'gate.stitchMinimalChange'
  // 表单校验
  | 'form.inputPathRequired'
  | 'form.outputPathRequired'
  | 'form.modelRequired'
  | 'form.apiUrlRequired';

export const messages: Record<MessageKey, string> = {
  'button.confirm': '确认继续',
  'button.reject': '驳回重写',
  'button.pause': '暂停',
  'button.resume': '恢复',
  'button.stop': '停止',
  'button.estimate': '预估本次',
  'button.start': '开始生成',
  'button.exportDiagnostics': '导出诊断包',
  'button.qualityReport': '质量报告',
  'button.history': '历史记录',
  'button.versions': '版本管理',
  'button.comparison': '对照稿',

  'gate.split.title': '拆书确认',
  'gate.fixReview.title': '断层勾选',
  'gate.blueprint.title': '蓝图确认',
  'gate.accept.title': '逐章验收',
  'gate.qc.title': '质检拦截',
  'gate.split.hint': '请确认章节拆分结果，识别不准可填写自定义章节标记后重拆。',
  'gate.blueprint.hint': '蓝图已生成，请确认或编辑后继续进入重构。',

  'status.idle': '空闲',
  'status.running': '运行中',
  'status.completed': '已完成',
  'status.paused': '已暂停',
  'status.error': '出错',
  'phase.phase0': '拆书',
  'phase.phase1': '诊断',
  'phase.phase2': '蓝图',
  'phase.phase3': '重构与缝合',
  'phase.phase4': '收尾',

  'gate.lockNames': '锁人名/设定名',
  'gate.lockPlot': '锁剧情骨架',
  'gate.injectForbidden': '注入禁改清单',
  'gate.requireChapterAccept': '逐章验收',
  'gate.skipStitchIfSmooth': '平滑时跳过缝合',
  'gate.qcBlockExport': '质检拦截导出',
  'gate.autoRewriteBadBatch': '自动重写坏批次',
  'gate.stitchMinimalChange': '缝合最小改动',

  'form.inputPathRequired': '请选择输入文件或目录',
  'form.outputPathRequired': '请选择输出目录',
  'form.modelRequired': '请填写模型名称',
  'form.apiUrlRequired': '请填写模型接口地址',
};

/** 取文案；后续支持 locale 切换时在此包一层 `currentLocale`。 */
export function t(key: MessageKey): string {
  return messages[key] ?? key;
}
