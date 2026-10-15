// 版本号单一来源：package.json 的 version。
//
// 前端由 Vite 构建期注入 __APP_VERSION__（见 vite.config.ts）；
// 后端由 Tauri 外壳运行时注入 TEXTFORGE_VERSION（见 src-tauri/src/backend.rs），
// 其值取自 Rust 的 CARGO_PKG_VERSION —— 也就是本脚本同步进 Cargo.toml 的版本。
// 于是在 package.json 改一次，界面 / 后端 / 安装包全链路一致。
//
// 由 npm 的 predev / prebuild 钩子自动调用；也可 `npm run sync-version` 手动跑。
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const version = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf-8')).version;
if (!version) {
  console.error('[sync-version] package.json 缺少 version');
  process.exit(1);
}

// 只替换「目标文件里第一次出现」的 version，避免误伤依赖项里的 version。
const targets = [
  {
    file: path.join(root, 'src-tauri', 'tauri.conf.json'),
    pattern: /("version"\s*:\s*")[^"]*(")/,
    replacer: `$1${version}$2`,
  },
  {
    // [package] 的 version 顶格书写；依赖的 version 都在行内/缩进，不会命中。
    file: path.join(root, 'src-tauri', 'Cargo.toml'),
    pattern: /^version\s*=\s*"[^"]*"/m,
    replacer: `version = "${version}"`,
  },
];

let changed = 0;
for (const { file, pattern, replacer } of targets) {
  const before = readFileSync(file, 'utf-8');
  const after = before.replace(pattern, replacer);
  if (after !== before) {
    writeFileSync(file, after);
    changed += 1;
    console.log(`[sync-version] 更新 ${path.relative(root, file)}`);
  }
}
console.log(`[sync-version] version=${version}（同步 ${changed} 个文件）`);
