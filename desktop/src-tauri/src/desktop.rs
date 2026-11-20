//! Desktop shell: the quick window (⌥ Space), the Menu Bar icon, launch at
//! login, and "closing the main window hides it".
//!
//! Everything here is driven from Rust, so the web view needs no extra
//! JavaScript packages or plugin permissions. The quick window calls only two
//! commands of its own (`hide_quick`, `open_main`); `open_main` accepts only
//! routes from a fixed list.

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

pub const MAIN: &str = "main";
pub const QUICK: &str = "quick";

/// Pages the quick window may open in the main window. Anything else is refused.
pub const ROUTES: [&str; 8] = [
    "/", "/chat", "/approvals", "/team", "/projects", "/knowledge", "/customize", "/settings",
];

/// `/chat/<uuid>` is allowed too, so "open this conversation" works.
pub fn allowed_route(path: &str) -> bool {
    if ROUTES.contains(&path) {
        return true;
    }
    match path.strip_prefix("/chat/") {
        Some(id) => id.len() == 36 && id.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-'),
        None => false,
    }
}

fn quick_shortcut() -> Shortcut {
    Shortcut::new(Some(Modifiers::ALT), Code::Space)
}

pub fn setup(app: &tauri::App) -> tauri::Result<()> {
    let handle = app.handle();

    WebviewWindowBuilder::new(app, QUICK, WebviewUrl::App("index.html".into()))
        .title("المساعد")
        .inner_size(420.0, 560.0)
        .resizable(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .visible(false)
        .center()
        .build()?;

    if let Some(quick) = app.get_webview_window(QUICK) {
        let hide = quick.clone();
        quick.on_window_event(move |event| {
            // Like Spotlight: clicking elsewhere puts it away.
            if let WindowEvent::Focused(false) = event {
                let _ = hide.hide();
            }
        });
    }
    if let Some(main) = app.get_webview_window(MAIN) {
        let keep = main.clone();
        main.on_window_event(move |event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // Quit lives in the Menu Bar; closing only hides.
                api.prevent_close();
                let _ = keep.hide();
            }
        });
    }

    handle.plugin(
        tauri_plugin_global_shortcut::Builder::new()
            .with_handler(|app, shortcut, event| {
                if shortcut == &quick_shortcut() && event.state == ShortcutState::Pressed {
                    toggle_quick(app);
                }
            })
            .build(),
    )?;
    if let Err(error) = handle.global_shortcut().register(quick_shortcut()) {
        // Another app may own ⌥ Space; the Menu Bar still opens the quick window.
        log::warn!("could not register the quick window shortcut: {error}");
    }

    handle.plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))?;
    build_tray(app)?;
    Ok(())
}

fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "افتح المساعد", true, None::<&str>)?;
    let quick = MenuItem::with_id(app, "quick", "النافذة السريعة (⌥ Space)", true, None::<&str>)?;
    let approvals = MenuItem::with_id(app, "approvals", "الموافقات", true, None::<&str>)?;
    let pause = MenuItem::with_id(app, "pause", "إيقاف مؤقت", true, None::<&str>)?;
    let login_on = app.autolaunch().is_enabled().unwrap_or(false);
    let login = CheckMenuItem::with_id(app, "login", "التشغيل مع الماك", true, login_on, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "خروج", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let separator2 = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&open, &quick, &approvals, &separator, &pause, &login, &separator2, &quit])?;

    let pause_item = pause.clone();
    let login_item = login.clone();
    // The icon itself is declared in tauri.conf.json (app.trayIcon, id
    // "assistant"); here we attach the menu and its actions.
    let Some(tray) = app.tray_by_id("assistant") else {
        log::warn!("Menu Bar icon not found; check app.trayIcon in tauri.conf.json");
        return Ok(());
    };
    tray.set_menu(Some(menu))?;
    tray.on_menu_event(move |app, event| match event.id().as_ref() {
        "open" => open_main_at(app, "/"),
        "quick" => toggle_quick(app),
        "approvals" => open_main_at(app, "/approvals"),
        "pause" => toggle_pause(app.clone(), pause_item.clone()),
        "login" => {
            let manager = app.autolaunch();
            let now = manager.is_enabled().unwrap_or(false);
            let result = if now { manager.disable() } else { manager.enable() };
            if let Err(error) = result {
                log::warn!("could not change launch at login: {error}");
            }
            let _ = login_item.set_checked(manager.is_enabled().unwrap_or(now));
        }
        "quit" => app.exit(0),
        _ => {}
    });
    Ok(())
}

pub fn toggle_quick(app: &AppHandle) {
    let Some(quick) = app.get_webview_window(QUICK) else { return };
    if quick.is_visible().unwrap_or(false) {
        let _ = quick.hide();
    } else {
        let _ = quick.center();
        let _ = quick.show();
        let _ = quick.set_focus();
    }
}

/// Shows the main window on one of the allowed routes.
pub fn open_main_at(app: &AppHandle, path: &str) {
    if !allowed_route(path) {
        log::warn!("refused route {path}");
        return;
    }
    let Some(main) = app.get_webview_window(MAIN) else { return };
    // `path` is from the fixed list or a checked UUID, so it is safe in the script.
    let _ = main.eval(format!("window.location.hash = '#{path}'"));
    let _ = main.show();
    let _ = main.unminimize();
    let _ = main.set_focus();
}

/// Pause or resume through the same token bridge the web view uses.
fn toggle_pause(app: AppHandle, item: MenuItem<tauri::Wry>) {
    tauri::async_runtime::spawn(async move {
        let bridge = app.state::<crate::Bridge>();
        let state = crate::api::send(&bridge.http, &bridge.config, "GET", "/api/v1/control", None).await;
        let paused = matches!(&state, Ok(r) if r.body.get("paused").and_then(|v| v.as_bool()) == Some(true));
        let path = if paused { "/api/v1/control/resume" } else { "/api/v1/control/pause" };
        match crate::api::send(&bridge.http, &bridge.config, "POST", path, None).await {
            Ok(response) if response.status == 200 => {
                let _ = item.set_text(if paused { "إيقاف مؤقت" } else { "استئناف المساعد" });
            }
            Ok(response) => log::warn!("pause toggle answered {}", response.status),
            Err(error) => log::warn!("pause toggle failed: {error}"),
        }
    });
}

#[tauri::command]
pub fn hide_quick(app: AppHandle) {
    if let Some(quick) = app.get_webview_window(QUICK) {
        let _ = quick.hide();
    }
}

#[tauri::command]
pub fn open_main(app: AppHandle, path: String) -> Result<(), String> {
    if !allowed_route(&path) {
        return Err(format!("Refused route: {path}"));
    }
    open_main_at(&app, &path);
    if let Some(quick) = app.get_webview_window(QUICK) {
        let _ = quick.hide();
    }
    Ok(())
}
