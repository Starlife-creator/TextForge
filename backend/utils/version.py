"""应用版本号的单一取值点。

真值来自 package.json：构建前由 scripts/sync-version.mjs 同步进
src-tauri/Cargo.toml 与 tauri.conf.json；运行时 Tauri 外壳把 CARGO_PKG_VERSION
经 TEXTFORGE_VERSION 环境变量注入 sidecar（见 src-tauri/src/backend.rs）。
因此在 package.json 改一次，后端与界面拿到的版本必然一致。

独立直跑（单元测试 / e2e / python main.py，未经过外壳）时没有该环境变量，
退回占位串 —— 这些场景都不校验版本号。
"""
import os

VERSION = os.environ.get("TEXTFORGE_VERSION") or "0.0.0-dev"
