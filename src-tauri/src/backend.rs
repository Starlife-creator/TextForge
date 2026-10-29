use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::OnceLock;
use std::time::Duration;
use serde::Serialize;
use tauri::AppHandle;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};

pub struct BackendProcess(pub Mutex<Option<CommandChild>>);

/// 后端 API 的调用方 token，仅存在于进程内存 + sidecar 环境变量，**从不落盘**。
///
/// 落盘等于公开：任何本地进程都读得到 data\port.json。放在这里只能由同进程的
/// 前端经 Tauri IPC 取到，别的本地进程和浏览器里的网页都拿不到。
static AUTH_TOKEN: OnceLock<String> = OnceLock::new();

/// 生成并返回本次运行的 token（幂等）。
pub fn auth_token() -> &'static str {
    AUTH_TOKEN.get_or_init(|| uuid::Uuid::new_v4().to_string())
}

/// 供前端取 token 的 Tauri 命令。走 IPC，不经过网络。
#[tauri::command]
pub fn get_backend_token() -> String {
    auth_token().to_string()
}

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
    let exe_dir = std::env::current_exe().ok()?;
    let exe_dir = exe_dir.parent()?;
    resolve_data_dir(
        std::env::var("TEXTFORGE_DATA_DIR").ok().as_deref(),
        Some(exe_dir),
        std::env::var("APPDATA").ok().as_deref(),
        std::env::var("HOME").ok().as_deref(),
        &|p: &std::path::Path| std::fs::create_dir_all(p).is_ok(),
    )
}

/// 数据目录解析（纯函数：输入全靠参数，三端 `cargo test` 都跑得到）。
///
/// 单一数据源：外壳先解析出数据目录，再通过 TEXTFORGE_DATA_DIR 注入给 sidecar，
/// 后端（utils/appdir.py）只认这个变量，认不到才退回 %APPDATA%\TextForge。
///
/// **「选中」的判据是能创建**（R9）：此前 `create_dir_all` 的失败被 `let _ =` 吞掉，
/// 环境变量照样注入。于是装进 Program Files（只读）时——外壳读
/// `exe旁/data/port.json`，后端因为创建失败回退到 `%APPDATA%\TextForge` 并写在那里，
/// 两边各算一处，前端永远等不到端口文件（表现为「打开就是空白界面」）。
/// 现在创建失败就换下一个候选，并且**只有真正落地的这个目录会被注入**，两侧必然一致。
///
/// 优先级：外部显式指定 > exe 旁的 data/（便携包，随包移动）> APPDATA（安装版）> 家目录。
fn resolve_data_dir(
    explicit: Option<&str>,
    exe_dir: Option<&std::path::Path>,
    appdata: Option<&str>,
    home: Option<&str>,
    ensure: &dyn Fn(&std::path::Path) -> bool,
) -> Option<PathBuf> {
    if let Some(p) = explicit.filter(|s| !s.is_empty()) {
        let path = PathBuf::from(p);
        if ensure(&path) {
            return Some(path);
        }
    }
    if let Some(dir) = exe_dir {
        let portable = dir.join("data");
        if ensure(&portable) {
            return Some(portable);
        }
    }
    if let Some(a) = appdata.filter(|s| !s.is_empty()) {
        let path = PathBuf::from(a).join("TextForge");
        // 最后一个候选不再试探：与后端 appdir.py 的兜底保持同一答案，
        // 全都创建不了时报错的是写入，而不是两侧各猜一个目录。
        let _ = ensure(&path);
        return Some(path);
    }
    home.filter(|s| !s.is_empty())
        .map(|h| PathBuf::from(h).join(".textforge"))
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

/// PID 是否还活着。用 OpenProcess 而非 tasklist 探测 —— 不 fork 子进程，且快。
pub fn pid_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };
        const STILL_ACTIVE: u32 = 259;
        unsafe {
            let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if h.is_null() {
                return false;
            }
            let mut code: u32 = 0;
            let ok = GetExitCodeProcess(h, &mut code) != 0;
            let _ = CloseHandle(h);
            ok && code == STILL_ACTIVE
        }
    }
    #[cfg(not(windows))]
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

/// 清理上一轮遗留的孤儿后端。
///
/// 为什么需要：正常关窗走 /api/system/shutdown 优雅退出（见 lib.rs），但如果外壳
/// **崩溃**或被任务管理器强杀，那条路径不会执行，backend 会带着 40MB 的 %TEMP%\_MEI
/// 目录一直驻留。启动时兜底收掉，避免反复崩溃后累积多个孤儿。
///
/// 只在端口文件里的 PID 仍存活且**端口探不通**时才动手 —— 宁可漏杀也不能误杀
/// 另一个正在使用的实例。
pub fn reap_orphan_backend() {
    let pid = get_pid_from_json();
    let port = get_port_from_json();
    if pid == 0 || !pid_alive(pid) {
        // 记录里的进程已经不在了：这份 port.json 指的是死端口。
        // 必须删掉，否则新后端启动前的任何一次读取都会把界面领到一个连不通的端口；
        // 但若端口真的有人应答（另一个没写 pid 的实例），留着它，别把活的服务判死。
        if port == 0 || !probe_port(port) {
            if let Some(p) = port_file_path() {
                if std::fs::remove_file(&p).is_ok() {
                    eprintln!("[backend] 端口文件已过期（pid 不在了），删除 {}", p.display());
                }
            }
        }
        return;
    }
    if port > 0 && probe_port(port) {
        return; // 还活着且服务可达 —— 是别人的实例，不碰
    }
    eprintln!("[backend] 清理上一轮遗留的孤儿进程 pid={pid}");
    kill_tree(pid);
    if let Some(p) = port_file_path() {
        let _ = std::fs::remove_file(p);
    }
}

/// 结束进程树。Windows 走 taskkill /T（连后代一起收）；
/// 其它平台退化为终止该 PID 本身（sidecar 是单进程，够用）。
///
/// child.kill() 只对 Tauri 直接拉起的那个进程生效；PyInstaller onefile 还会再拉起
/// 一个跑 Python 的子进程，于是子进程变孤儿继续运行。taskkill /T 连后代一起收掉。
/// 失败静默忽略 —— 这只是 /api/shutdown 之后的兜底路径。
pub fn kill_tree(pid: u32) {
    #[cfg(windows)]
    {
        let _ = std::process::Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .output();
    }
    #[cfg(not(windows))]
    {
        unsafe { libc::kill(pid as i32, libc::SIGKILL) };
    }
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
    // data_dir() 已经把选中的目录创建好（创建不了就换下一个候选），所以这里注入的
    // 一定是两侧真正都能用的那一个；过去是「创建失败也照样注入」，后端随后回退，
    // 两边就分叉了（R9）。
    if let Some(dir) = data_dir() {
        cmd = cmd.env("TEXTFORGE_DATA_DIR", dir);
    }
    // 注入调用方 token：后端所有接口（/api/health 除外）都会校验 X-TextForge-Token，
    // 挡住「任何本地进程读 port.json 就能调 /api/start 读写任意路径」这一攻击面。
    cmd = cmd.env("TEXTFORGE_TOKEN", auth_token());
    // 注入版本号：单一来源是 package.json，经 sync-version 同步到 Cargo.toml，
    // 这里把编译期 crate 版本传给后端，界面与 /api/health / /api/system/info 一致。
    cmd = cmd.env("TEXTFORGE_VERSION", env!("CARGO_PKG_VERSION"));
    let (mut rx, child) = cmd.spawn()
        .map_err(|e| format!("sidecar 启动失败: {}", e))?;
    let child_pid = child.pid();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => eprintln!("[backend stdout] {}", String::from_utf8_lossy(&line)),
                CommandEvent::Stderr(line) => eprintln!("[backend stderr] {}", String::from_utf8_lossy(&line)),
                CommandEvent::Terminated(payload) => {
                    eprintln!("[backend] 进程退出: {:?}", payload);
                    // 只清理「本子进程」写下的 port.json。restart_backend 是
                    // 「kill 旧子进程 → 删 port.json → spawn 新进程」，而 Terminated
                    // 事件异步投递：旧进程的退出事件可能在新进程刚写好 port.json、
                    // 但**尚未 listen** 的窗口才被处理，此时按端口探测会把新进程误判成
                    // 死进程而删掉文件 → 前端对着空端口空等 30s。
                    // 用 PID 绑定即可精确区分，绝不误删别人的 port.json。
                    if get_pid_from_json() != child_pid {
                        eprintln!("[backend] port.json 已属于更新的进程，跳过清理");
                        continue;
                    }
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

/// 必须是 async：probe_port 最长阻塞 800ms，同步命令会卡住主线程（同 backend_info）。
///
/// 「port.json 存在」不等于「后端听得见」。孤儿进程被强杀、或端口文件来自上一轮，
/// 都会留下一个连不通的端口；过去这里直接返回 Ready，前端拿着死端口发请求，
/// 于是 waitForPort 第一次就成功返回、后续每个调用都失败（R9 的闩锁）。
/// 现在探不通就继续 NotReady，让 waitForPort 的轮询等到服务真的起来。
#[tauri::command]
pub async fn get_backend_port() -> PortResult {
    if cfg!(debug_assertions) { return PortResult::DevMode(8000); }
    let Some(path) = port_file_path() else { return PortResult::Error("无法确定数据目录".into()); };
    if !path.exists() { return PortResult::NotReady; }
    let port = match std::fs::read_to_string(&path) {
        Ok(content) => match serde_json::from_str::<serde_json::Value>(&content) {
            Ok(json) => match json["port"].as_u64() {
                Some(p) => p as u16,
                None => return PortResult::Error("port 字段缺失".into()),
            },
            Err(e) => return PortResult::Error(format!("解析失败: {}", e)),
        },
        Err(e) => return PortResult::Error(format!("读取失败: {}", e)),
    };
    if port == 0 { return PortResult::NotReady; }
    let alive = tokio::task::spawn_blocking(move || probe_port(port))
        .await
        .unwrap_or(false);
    classify_port(port, alive)
}

/// 端口文件里的端口 + TCP 探测结果 ⇒ 该不该把端口交给前端（纯函数，便于测试）。
/// 判据只有一条：**听得见才算 Ready**（R9 的死端口闩锁）。
fn classify_port(port: u16, reachable: bool) -> PortResult {
    if port != 0 && reachable {
        PortResult::Ready(port)
    } else {
        PortResult::NotReady
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
        .header(TOKEN_HEADER, auth_token())
        .json(&serde_json::json!({ "force": true }))
        .send()
        .await;
    tokio::time::sleep(Duration::from_millis(600)).await;
}

/// 外壳 -> 后端的 HTTP 调用**必须**带调用方 token。
///
/// 后端在 release 下会对所有接口（/api/health 除外）校验 X-TextForge-Token。
/// 漏带的后果很隐蔽：不带 token 时关窗的优雅退出请求会被 401 静默拒绝，
/// 于是流程退化到强杀分支，孤儿进程与 %TEMP%\_MEI 残留又回来了 ——
/// 而端到端测试是直接起 python、不经外壳，**根本覆盖不到这条路径**。
pub const TOKEN_HEADER: &str = "X-TextForge-Token";

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
// ---------------------------------------------------------------------------
// 单元测试
//
// 这层此前 0 覆盖，却承载着进程生命周期、端口发现、数据目录解析 —— 开发时在这里
// 连续踩了三个坑（孤儿进程、_MEI 临时目录残留、kill 顺序），全靠手工点窗口复现。
// 下面把纯逻辑钉死；涉及真实进程的部分留给集成测试。
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn auth_token_is_idempotent_and_strong() {
        let a = auth_token();
        let b = auth_token();
        assert_eq!(a, b, "token must be generated only once; otherwise the value                             injected into the sidecar differs from what the UI reads");
        assert_eq!(a.len(), 36, "UUID v4 text form is always 36 chars");
        assert!(a.chars().all(|c| c.is_ascii_hexdigit() || c == '-'));
    }

    #[test]
    fn auth_token_is_never_written_to_disk() {
        // 防回归：token 一旦进了 port.json，任何本地进程都读得到，鉴权形同虚设。
        let t = auth_token().to_string();
        if let Some(pf) = port_file_path() {
            if let Ok(content) = std::fs::read_to_string(&pf) {
                assert!(!content.contains(&t), "token must never appear in port.json");
            }
        }
    }

    #[test]
    fn port_file_lives_under_data_dir() {
        // port.json 必须住在 data_dir() 里 —— 这是 Rust 与 Python 唯一对得上的位置。
        // 不再硬编码 current_exe()/data：现在数据目录取决于「哪一层先能创建」。
        let pf = port_file_path().expect("测试环境总能解析出一个数据目录");
        assert_eq!(pf.file_name().unwrap(), "port.json");
        assert_eq!(pf.parent().unwrap(), data_dir().unwrap());
    }

    #[test]
    fn data_dir_prefers_the_portable_side_when_it_is_writable() {
        let got = resolve_data_dir(
            None,
            Some(std::path::Path::new("/opt/textforge")),
            Some("C:/Users/me/AppData/Roaming"),
            Some("/home/me"),
            &|_| true,
        );
        assert_eq!(got, Some(PathBuf::from("/opt/textforge").join("data")));
    }

    #[test]
    fn data_dir_falls_back_to_appdata_when_the_install_dir_is_readonly() {
        // R9 的真实场景：装进 Program Files。旧实现把创建失败用 `let _ =` 吞掉，
        // 照样注入 TEXTFORGE_DATA_DIR ⇒ 后端回退到 %APPDATA%、外壳却仍读 exe 旁，
        // 两边各算一处 ⇒ 前端永远等不到 port.json（打开就是空白界面）。
        let got = resolve_data_dir(
            None,
            Some(std::path::Path::new("C:/Program Files/TextForge")),
            Some("C:/Users/me/AppData/Roaming"),
            Some("C:/Users/me"),
            &|p| !p.starts_with("C:/Program Files"),
        );
        assert_eq!(got, Some(PathBuf::from("C:/Users/me/AppData/Roaming").join("TextForge")));
    }

    #[test]
    fn data_dir_ignores_an_override_it_cannot_create() {
        // 显式指定也必须「创建得了才算数」：注入一个建不了的目录，后端会自己回退，
        // 分叉就回来了。
        let got = resolve_data_dir(
            Some("E:/locked/TextForge"),
            Some(std::path::Path::new("/app")),
            None,
            Some("/home/me"),
            &|p| p == PathBuf::from("/app").join("data"),
        );
        assert_eq!(got, Some(PathBuf::from("/app").join("data")));
    }

    #[test]
    fn data_dir_without_appdata_uses_the_home_dir() {
        // 与后端 appdir.py 的最后一档（~/.textforge）保持一致，别让两侧又分叉。
        let got = resolve_data_dir(None, None, None, Some("/home/me"), &|_| true);
        assert_eq!(got, Some(PathBuf::from("/home/me").join(".textforge")));
    }

    #[test]
    fn data_dir_rejects_an_empty_override_instead_of_treating_it_as_a_path() {
        let got = resolve_data_dir(
            Some(""),
            Some(std::path::Path::new("/app")),
            None,
            None,
            &|_| true,
        );
        assert_eq!(got, Some(PathBuf::from("/app").join("data")));
    }

    #[test]
    fn probe_port_rejects_a_just_released_port() {
        // 「port.json 存在」≠「听得见」：get_backend_port 靠这条把死端口挡在外面。
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        assert!(!probe_port(port), "刚释放的端口不该被判成活着");
    }

    #[test]
    fn probe_port_accepts_a_listening_port() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(probe_port(port), "真在听的端口必须判成活着，否则 waitForPort 永远等不完");
    }

    #[test]
    fn a_dead_port_is_never_handed_to_the_frontend() {
        // R9 闩锁：port.json 里的端口听不见时必须是 NotReady（让轮询继续等），
        // 不能 Ready —— 前端拿到死端口后 waitForPort 就「成功」了，之后每个请求都失败。
        assert!(matches!(classify_port(53117, false), PortResult::NotReady));
        assert!(matches!(classify_port(0, true), PortResult::NotReady));
        assert!(matches!(classify_port(53117, true), PortResult::Ready(53117)));
    }

    #[test]
    fn pid_zero_is_not_alive() {
        assert!(!pid_alive(0), "PID 0 is not a valid process");
    }

    #[test]
    fn own_pid_is_alive() {
        assert!(pid_alive(std::process::id()), "the running test process must be alive");
    }

    #[test]
    fn nonexistent_pid_is_dead() {
        assert!(!pid_alive(0x7FFF_FF00), "an unused high PID must report dead");
    }

    #[test]
    fn probe_port_zero_fails() {
        assert!(!probe_port(0), "port 0 is meaningless and must fail fast");
    }

    #[test]
    fn missing_port_file_yields_zero_without_panic() {
        // 启动早期 port.json 还不存在是常态，绝不能 panic
        let _ = get_port_from_json();
        let _ = get_pid_from_json();
    }

    #[test]
    fn kill_tree_tolerates_dead_pid() {
        // reap_orphan_backend 会对过期 PID 调用它，必须容错
        kill_tree(0x7FFF_FF00);
    }

    #[test]
    fn token_header_name_matches_backend_constant() {
        // 两端各写了一份头名常量。一旦拼错，release 下外壳的关窗请求会被 401
        // 静默拒绝 -> 优雅退出退化成强杀 -> 孤儿进程与 _MEI 残留回归，
        // 而端到端测试直接起 python、根本覆盖不到这条路径。
        assert_eq!(TOKEN_HEADER, "X-TextForge-Token");
        assert!(!auth_token().is_empty());
    }
}
