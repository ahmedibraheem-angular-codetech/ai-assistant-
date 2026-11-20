fn main() {
    // Declaring the app commands makes them deny-by-default: a window can only
    // call them when its capability file grants them (capabilities/default.json).
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["api_request", "api_health", "hide_quick", "open_main"]),
        ),
    )
    .expect("failed to run tauri-build");
}
