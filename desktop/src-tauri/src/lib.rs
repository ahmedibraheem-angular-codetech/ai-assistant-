pub mod api;
pub mod desktop;
pub mod notify;
pub mod services;

use api::{ApiConfig, ApiResponse};
use serde_json::Value;
use tauri::Manager;

pub struct Bridge {
    pub config: ApiConfig,
    pub http: reqwest::Client,
}

/// The only way the web view reaches the API. The token stays in Rust.
#[tauri::command]
async fn api_request(
    bridge: tauri::State<'_, Bridge>,
    method: String,
    path: String,
    body: Option<Value>,
) -> Result<ApiResponse, String> {
    // Method and path only: never the body or the token.
    log::info!("api_request {method} {path}");
    let result = api::send(&bridge.http, &bridge.config, &method, &path, body).await;
    match &result {
        Ok(response) => log::info!("api_request {path} -> {}", response.status),
        Err(error) => log::warn!("api_request {path} failed: {error}"),
    }
    result
}

/// Public health check, used for the "server online" indicator.
#[tauri::command]
async fn api_health(bridge: tauri::State<'_, Bridge>) -> Result<bool, String> {
    Ok(api::health(&bridge.http, &bridge.config).await)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let bridge = Bridge {
        config: ApiConfig::from_env(),
        http: api::client().expect("HTTP client"),
    };
    let starter = (bridge.http.clone(), bridge.config.clone());
    let notifier = starter.clone();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .manage(bridge)
        .manage(services::Services::default())
        .invoke_handler(tauri::generate_handler![api_request, api_health, desktop::hide_quick, desktop::open_main])
        .setup(|app| {
            // Logging first, so warnings from the desktop setup are not lost.
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            desktop::setup(app)?;
            // Start Ollama and the assistant server if they are not running.
            let handle = app.handle().clone();
            let (http, config) = starter;
            tauri::async_runtime::spawn(async move {
                let services = handle.state::<services::Services>();
                services::ensure_all(http, config, &services).await;
            });
            let (http, config) = notifier;
            tauri::async_runtime::spawn(notify::run(app.handle().clone(), http, config));
            log::info!("desktop ready: quick window, Menu Bar icon, shortcut");
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Tauri application");
    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            // Stop the server only if this app started it (not the login item).
            handle.state::<services::Services>().stop();
        }
    });
}
