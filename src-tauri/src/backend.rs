use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use serde::Serialize;
use tauri::AppHandle;
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

/// 后端运行时快照，供前端「系统与后端设置」面板展示。
/// 与 backend.rs:restart_backend 配合，取代此前「只能靠报错文案猜后端状态」的黑盒体验。
#[derive(Serialize)]
pub struct BackendInfo {
    /// 端口文件里记录的端口（0 = 尚无端口文件）
    port: u16,
    /// 端口文件里记录的 PID（0 = 无）
    pid: u32,
    /// 对该端口做 TCP 探测是否真的连得上（端口文件可能过期 —— 孤儿进程/僵尸文件）
    reachable: bool,
    /// 子进程句柄是否还在（release 模式才有）
    child_alive: bool,
    /// 是否 debug 构建（debug 下没有 sidecar，需手动跑 python）
    debug: bool,
    /// port.json 路径
    port_file: String,
    /// 数据目录：便携包为 exe 旁 data/，安装版为 %APPDATA%\TextForge
    app_dir: String,
    /// 数据目录下的 logs/backend.log
    log_path: String,
    log_exists: bool,
}

pub fn data_dir() -> Option<PathBuf> {
    // 单一数据源：外壳先解析出数据目录，再通过 TEXTFORGE_DATA_DIR 注入给 sidecar。
    // 后端（utils/appdir.py）只认这个环境变量，认不到才退回 %APPDATA%\TextForge。
    // 这样两边**不可能**算出不同路径 —— 原先两侧各自拼 %APPDATA%\TextForge，
    // 一旦不一致，前端就永远读不到 port.json。
    //
    // 优先级：外部显式指定 > exe 旁的 data/（便携包，随包移动）> %APPDATA%（安装版）。
    if let Ok(p) = std::env::var("TEXTFORGE_DATA_DIR") {
        if !p.is_empty() {
            return Some(PathBuf::from(p));
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            return Some(dir.join("data"));
        }
    }
    std::env::var("APPDATA").ok().map(|p| PathBuf::from(p).join("TextForge"))
}

pub fn port_file_path() -> Option<PathBuf> {
    data_dir().map(|d| d.join("port.json"))
}

pub fn get_port_from_json() -> u16 {
    port_file_path()
        .and_then(|p| std::fs::read_to_string(&p).ok())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .and_then(|j| j["port"].as_u64())
        .map(|p| p as u16)
        .unwrap_or(0)
}

fn get_pid_from_json() -> u32 {
    port_file_path()
        .and_then(|p| std::fs::read_to_string(&p).ok())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .and_then(|j| j["pid"].as_u64())
        .map(|p| p as u32)
        .unwrap_or(0)
}

/// TCP 探测：端口文件存在 ≠ 进程活着。用它把「有 port.json 但连不上」区分出来。
fn probe_port(port: u16) -> bool {
    if port == 0 {
        return false;
    }
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    TcpStream::connect_timeout(&addr, Duration::from_millis(800)).is_ok()
}

pub fn spawn_backend(app: &AppHandle) -> Result<CommandChild, String> {
    let sidecar = app.shell().sidecar("backend")
        .map_err(|e| format!("sidecar 创建失败: {}", e))?;
    let mut cmd = sidecar;
    // 把解析好的数据目录交给后端，避免两侧各算一套路径。
    if let Some(dir) = data_dir() {
        let _ = std::fs::create_dir_all(&dir);
        cmd = cmd.env("TEXTFORGE_DATA_DIR", dir);
    }
    let (mut rx, child) = cmd.spawn()
        .map_err(|e| format!("sidecar 启动失败: {}", e))?;
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => eprintln!("[backend stdout] {}", String::from_utf8_lossy(&line)),
                CommandEvent::Stderr(line) => eprintln!("[backend stderr] {}", String::from_utf8_lossy(&line)),
                CommandEvent::Terminated(payload) => {
                    eprintln!("[backend] 进程退出: {:?}", payload);
                    // 只有当 port.json 记录的端口确实没人监听时才删。
                    // restart_backend 是「先 kill 旧子进程、再 spawn 新进程」，而
                    // Terminated 事件异步投递 —— 无条件删除会把新进程刚写好的
                    // port.json 抹掉，前端只能对着空端口空等 30s。
                    //
                    // 用 spawn_blocking：probe_port 最多阻塞 800ms，不能占着
                    // async runtime 的 worker 线程（退出和重启都会走到这里）。
                    let p = get_port_from_json();
                    // unwrap_or(true) = 保守：探测本身出错时**保留** port.json。
                    // 误判成「已死」就会删掉前端唯一能拿到端口的线索，
                    // 让它对着空端口空等 30s —— 这正是本段注释要防的事。
                    let alive = match tauri::async_runtime::spawn_blocking(move || probe_port(p)).await {
                        Ok(v) => v,
                        Err(e) => { eprintln!("[backend] 端口探测失败，保守保留 port.json: {:?}", e); true }
                    };
                    if !alive {
                        if let Some(path) = port_file_path() { let _ = std::fs::remove_file(path); }
                    }
                }
                _ => {}
            }
        }
    });
    Ok(child)
}

#[tauri::command]
pub fn get_backend_port() -> PortResult {
    if cfg!(debug_assertions) { return PortResult::DevMode(8000); }
    let Some(path) = port_file_path() else { return PortResult::Error("无法确定数据目录".into()); };
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

/// 后端运行时快照，供前端「系统与后端设置」面板展示。
/// 必须是 async：probe_port 最长阻塞 800ms，同步命令会在主线程上卡住整个
/// WebView —— SettingsModal 每 5s 调一次，后端挂掉时会持续掉帧。
#[tauri::command]
pub async fn backend_info(state: tauri::State<'_, BackendProcess>) -> Result<BackendInfo, String> {
    let port = get_port_from_json();
    let child_alive = state.0.lock().map(|g| g.is_some()).unwrap_or(false);
    let reachable = tokio::task::spawn_blocking(move || probe_port(port))
        .await
        .unwrap_or(false);
    let dir = data_dir();
    let log_path = dir.as_ref().map(|d| d.join("logs").join("backend.log"));
    let log_exists = log_path.as_ref().map(|p| p.exists()).unwrap_or(false);
    Ok(BackendInfo {
        port,
        pid: get_pid_from_json(),
        reachable,
        child_alive,
        debug: cfg!(debug_assertions),
        port_file: port_file_path().map(|p| p.display().to_string()).unwrap_or_default(),
        app_dir: dir.map(|d| d.display().to_string()).unwrap_or_default(),
        log_path: log_path.map(|p| p.display().to_string()).unwrap_or_default(),
        log_exists,
    })
}

/// 请旧后端自行退出，解决「sidecar 句柄丢了 → kill 无效 → 孤儿进程占着端口」。
/// 失败无所谓：紧接着的 child.kill() 与删端口文件会兜底。
async fn graceful_shutdown(port: u16) {
    if port == 0 {
        return;
    }
    let Ok(client) = reqwest::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
    else {
        return;
    };
    let _ = client
        .post(format!("http://127.0.0.1:{port}/api/system/shutdown"))
        .json(&serde_json::json!({ "force": true }))
        .send()
        .await;
    tokio::time::sleep(Duration::from_millis(600)).await;
}

#[tauri::command]
pub async fn restart_backend(app: AppHandle, state: tauri::State<'_, BackendProcess>) -> Result<(), String> {
    if cfg!(debug_assertions) {
        return Err("开发模式没有 sidecar，请在终端手动重启：python backend/main.py".into());
    }
    // 1) 先让旧进程自己退（覆盖 handle 已失效的孤儿场景）
    graceful_shutdown(get_port_from_json()).await;
    // 2) 再 kill 句柄内的子进程
    let existing = state.0.lock().ok().and_then(|mut g| g.take());
    if let Some(child) = existing {
        if let Err(e) = child.kill() { eprintln!("[backend] kill 失败: {:?}", e); }
    }
    // 3) 清端口文件，避免前端对着死端口空等 30s
    if let Some(path) = port_file_path() { let _ = std::fs::remove_file(path); }
    // 4) 重新拉起
    match spawn_backend(&app) {
        Ok(child) => {
            if let Ok(mut g) = state.0.lock() { *g = Some(child); }
            Ok(())
        }
        Err(e) => Err(format!("重启失败: {}，请手动重启应用", e)),
    }
}