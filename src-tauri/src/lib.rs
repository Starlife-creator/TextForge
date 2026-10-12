mod backend;
use backend::{BackendProcess, get_backend_port, backend_info, restart_backend, port_file_path, get_port_from_json, spawn_backend};
use std::sync::Mutex;
use tauri::Manager;

/// 按**进程树**结束进程（仅 Windows）。
///
/// child.kill() 只对 Tauri 直接拉起的那个进程生效；PyInstaller onefile 还会
/// 再拉起一个跑 Python 的子进程，于是子进程变孤儿继续运行。taskkill /T 连后代
/// 一起收掉。失败静默忽略 —— 这只是 /api/shutdown 之后的兜底路径。
fn kill_tree(pid: u32) {
    #[cfg(windows)]
    {
        let _ = std::process::Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .output();
    }
    #[cfg(not(windows))]
    let _ = pid;
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") { let _ = w.set_focus(); }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .manage(BackendProcess(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![get_backend_port, backend_info, restart_backend])
        .setup(|app| {
            if !cfg!(debug_assertions) {
                let child = spawn_backend(app.handle())?;
                *app.state::<BackendProcess>().0.lock().unwrap() = Some(child);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let app = window.app_handle().clone();
                tauri::async_runtime::spawn(async move {
                    let port = get_port_from_json();
                    let mut graceful = false;
                    let client = reqwest::Client::builder()
                        .timeout(std::time::Duration::from_secs(5))
                        .build().unwrap();
                    if port > 0 {
                        // 先停流水线，再让它整个服务退出。
                        let _ = client.post(format!("http://127.0.0.1:{}/api/stop", port)).send().await;
                        // 复用既有的 /api/system/shutdown（设置面板「停止后端」同一个端点），
                        // 不新增重复路由。让后端自己 os._exit，PyInstaller 引导进程
                        // 随之退出并清掉 %TEMP%\_MEIxxx。
                        let _ = client.post(format!("http://127.0.0.1:{}/api/system/shutdown", port)).send().await;

                        // **轮询**等后端真的下线，而不是固定 sleep。
                        // 只有优雅退出（uvicorn should_exit → 子进程退出 → PyInstaller
                        // 引导进程自行退出）才会清掉 %TEMP%\_MEIxxx 里的约 40MB 解压产物；
                        // 一旦走到强杀，引导进程来不及清理就会永久留下这个目录。
                        let mut down = false;
                        for _ in 0..24 {
                            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                            if client
                                .get(format!("http://127.0.0.1:{}/api/health", port))
                                .send().await.is_err()
                            {
                                down = true;
                                break;
                            }
                        }
                        if down {
                            graceful = true;
                            // 再给引导进程一点时间收尾并删除 _MEI 目录
                            tokio::time::sleep(std::time::Duration::from_millis(1200)).await;
                        }
                    }
                    {
                        // 兜底：优雅路径超时（端口读不到、请求全失败、被杀等）才强杀。
                        // 顺序上必须**先**按进程树杀再 child.kill()：反过来先 kill 掉
                        // 父引导进程，父子关系就断了，taskkill /T 找不到后代，
                        // PyInstaller 拉起的 Python 子进程会变成孤儿继续跑。
                        //
                        // 优雅成功时**不能**再 taskkill：pid 可能已被系统回收给
                        // 别的进程，/T /F 会误杀无关的进程树。
                        let state = app.state::<BackendProcess>();
                        let taken = state.0.lock().unwrap().take();
                        drop(state);
                        if let Some(child) = taken {
                            let pid = child.pid();
                            if !graceful {
                                eprintln!("[backend] 优雅退出未完成，按进程树强杀 pid={pid}");
                                kill_tree(pid);
                                if let Err(e) = child.kill() {
                                    eprintln!("[backend] kill 失败({pid}): {e:?}");
                                }
                            }
                            // graceful 时**绝不能**再 kill：引导进程正在自己删除
                            // %TEMP%\_MEIxxx，这一步把它打断就会永久留下约 40MB 临时目录。
                        }
                    }
                    if let Some(path) = port_file_path() { let _ = std::fs::remove_file(path); }
                    app.exit(0);
                    std::process::exit(0);
                });
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
