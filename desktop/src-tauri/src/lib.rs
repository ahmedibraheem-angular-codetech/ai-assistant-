pub mod api;

use api::{ApiConfig, ApiResponse};
use serde_json::Value;

struct Bridge {
    config: ApiConfig,
    http: reqwest::Client,
}

/// The only way the web view reaches the API. The token stays in Rust.
#[tauri::command]
async fn api_request(
    bridge: tauri::State<'_, Bridge>,
    method: String,
    path: String,
    body: Option<Value>,
) -> Result<ApiResponse, String> {
    api::send(&bridge.http, &bridge.config, &method, &path, body).await
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
    tauri::Builder::default()
        .manage(bridge)
        .invoke_handler(tauri::generate_handler![api_request, api_health])
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running the Tauri application");
}
