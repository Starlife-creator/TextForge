use std::path::PathBuf;
use std::sync::Mutex;
use serde::Serialize;
use tauri::{AppHandle};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};

pub struct BackendProcess(pub Mutex<Option<CommandChild>>);

#[derive(Serialize)]
#[serde(tag = "status", content = "data")]
pub enum PortResult {
    Ready(u16),
    NotReady,
    DevMode(u16),
    Error(String),
}

pub fn port_file_path() -> Option<PathBuf> {
    std::env::var("APPDATA").ok()
        .map(|p| PathBuf::from(p).join("TextForge").join("port.json"))
}

pub fn get_port_from_json() -> u16 {
    port_file_path()
        .and_then(|p| std::fs::read_to_string(&p).ok())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .and_then(|j| j["port"].as_u64())
        .map(|p| p as u16)
        .unwrap_or(0)
}

pub fn spawn_backend(app: &AppHandle) -> Result<CommandChild, String> {
    let sidecar = app.shell().sidecar("backend")
        .map_err(|e| format!("sidecar 创建失败: {}", e))?;
    let (mut rx, child) = sidecar.spawn()
        .map_err(|e| format!("sidecar 启动失败: {}", e))?;
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => eprintln!("[backend stdout] {}", String::from_utf8_lossy(&line)),
                CommandEvent::Stderr(line) => eprintln!("[backend stderr] {}", String::from_utf8_lossy(&line)),
                CommandEvent::Terminated(payload) => eprintln!("[backend] 进程退出: {:?}", payload),
                _ => {}
            }
        }
    });
    Ok(child)
}

#[tauri::command]
pub fn get_backend_port() -> PortResult {
    if cfg!(debug_assertions) { return PortResult::DevMode(8000); }
    let Some(path) = port_file_path() else { return PortResult::Error("APPDATA 缺失".into()); };
    if !path.exists() { return PortResult::NotReady; }
    match std::fs::read_to_string(&path) {
        Ok(content) => match serde_json::from_str::<serde_json::Value>(&content) {
            Ok(json) => match json["port"].as_u64() {
                Some(p) => PortResult::Ready(p as u16),
                None => PortResult::Error("port 字段缺失".into()),
            },
            Err(e) => PortResult::Error(format!("解析失败: {}", e)),
        },
        Err(e) => PortResult::Error(format!("读取失败: {}", e)),
    }
}

#[tauri::command]
pub fn restart_backend(app: AppHandle, state: tauri::State<BackendProcess>) -> Result<(), String> {
    if cfg!(debug_assertions) { return Err("开发模式请手动重启 python backend/main.py".into()); }
    if let Some(child) = state.0.lock().unwrap().take() {
        if let Err(e) = child.kill() { eprintln!("[backend] kill 失败: {:?}", e); }
    }
    if let Some(path) = port_file_path() { let _ = std::fs::remove_file(path); }
    match spawn_backend(&app) {
        Ok(child) => { *state.0.lock().unwrap() = Some(child); Ok(()) }
        Err(e) => Err(format!("重启失败: {}，请手动重启应用", e)),
    }
}
