mod backend;
use backend::{BackendProcess, get_backend_port, restart_backend, port_file_path, get_port_from_json, spawn_backend};
use std::sync::Mutex;
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") { let _ = w.set_focus(); }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .manage(BackendProcess(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![get_backend_port, restart_backend])
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
                    if port > 0 {
                        let client = reqwest::Client::builder()
                            .timeout(std::time::Duration::from_secs(60))
                            .build().unwrap();
                        let _ = client.post(format!("http://127.0.0.1:{}/api/stop", port)).send().await;
                        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                    }
                    {
                        let state = app.state::<BackendProcess>();
                        if let Some(child) = state.0.lock().unwrap().take() {
                            if let Err(e) = child.kill() { eprintln!("[backend] kill 失败: {:?}", e); }
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
