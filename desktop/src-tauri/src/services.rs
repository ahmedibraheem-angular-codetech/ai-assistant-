//! Starts the local servers the app needs, when they are not already running.
//!
//! Two servers today, both on loopback:
//! - Ollama (the local model), `ollama serve` on 11434. Left running when the
//!   app quits, because other tools may share it.
//! - The assistant API on `ApiConfig::port`. If the login item
//!   `com.personal-assistant.server` is installed, the app asks launchd to
//!   start it (`launchctl kickstart`), so there is never a second copy fighting
//!   for the serve lease. Otherwise the app starts `.venv/bin/python -m app`
//!   itself and stops it on quit.
//!
//! Set `ASSISTANT_MANAGE_SERVERS=0` to turn this off (for example when the API
//! runs on another machine later). `ASSISTANT_SERVER_DIR` overrides where the
//! Python project is (default: the folder above the data folder).

use std::fs::{self, File, OpenOptions};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use crate::api::{self, ApiConfig};

pub const AGENT_LABEL: &str = "com.personal-assistant.server";
pub const OLLAMA_URL: &str = "http://127.0.0.1:11434/api/version";
const READY_TIMEOUT: Duration = Duration::from_secs(40);

#[derive(Debug, PartialEq, Eq)]
pub enum ApiStart {
    AlreadyUp,
    Kickstart,
    Spawn,
}

/// How to bring the API up. Pure, so it can be tested.
pub fn plan_api_start(api_up: bool, agent_installed: bool) -> ApiStart {
    match (api_up, agent_installed) {
        (true, _) => ApiStart::AlreadyUp,
        (false, true) => ApiStart::Kickstart,
        (false, false) => ApiStart::Spawn,
    }
}

/// `ASSISTANT_MANAGE_SERVERS`: anything but 0/off/false/no means "manage".
pub fn managed_from(value: Option<&str>) -> bool {
    let value = value.map(|v| v.trim().to_ascii_lowercase()).unwrap_or_default();
    !matches!(value.as_str(), "0" | "off" | "false" | "no")
}

pub fn server_dir_from(data_dir: &Path, value: Option<&str>) -> PathBuf {
    match value.map(str::trim).filter(|v| !v.is_empty()) {
        Some(dir) => PathBuf::from(dir),
        None => data_dir.parent().map(Path::to_path_buf).unwrap_or_else(|| data_dir.to_path_buf()),
    }
}

/// First candidate that is an executable regular file.
pub fn find_binary(candidates: &[PathBuf]) -> Option<PathBuf> {
    candidates.iter().find(|path| {
        fs::metadata(path)
            .map(|info| info.is_file() && info.permissions().mode() & 0o111 != 0)
            .unwrap_or(false)
    }).cloned()
}

fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/tmp"))
}

fn agent_plist() -> PathBuf {
    home().join("Library/LaunchAgents").join(format!("{AGENT_LABEL}.plist"))
}

fn log_file(name: &str) -> Option<File> {
    let dir = home().join("Library/Logs/personal-assistant");
    fs::create_dir_all(&dir).ok()?;
    OpenOptions::new().create(true).append(true).open(dir.join(name)).ok()
}

fn quiet_output(name: &str) -> (Stdio, Stdio) {
    match log_file(name).and_then(|file| Some((file.try_clone().ok()?, file))) {
        Some((out, err)) => (Stdio::from(out), Stdio::from(err)),
        None => (Stdio::null(), Stdio::null()),
    }
}

/// The API child the app started itself, stopped on quit.
#[derive(Default)]
pub struct Services {
    api_child: Mutex<Option<Child>>,
}

impl Services {
    pub fn stop(&self) {
        if let Ok(mut slot) = self.api_child.lock() {
            if let Some(mut child) = slot.take() {
                // SIGTERM first, so uvicorn shuts down and frees the serve lease.
                // SAFETY: kill with a pid we own; failure is harmless.
                unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) };
                for _ in 0..30 {
                    if matches!(child.try_wait(), Ok(Some(_))) {
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

async fn up(http: &reqwest::Client, url: &str) -> bool {
    matches!(http.get(url).timeout(Duration::from_secs(2)).send().await, Ok(r) if r.status().is_success())
}

async fn wait_until<F, Fut>(check: F) -> bool
where
    F: Fn() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    let deadline = std::time::Instant::now() + READY_TIMEOUT;
    while std::time::Instant::now() < deadline {
        if check().await {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    false
}

async fn ensure_ollama(http: &reqwest::Client) {
    if up(http, OLLAMA_URL).await {
        return;
    }
    let candidates = [
        PathBuf::from("/opt/homebrew/bin/ollama"),
        PathBuf::from("/usr/local/bin/ollama"),
        PathBuf::from("/Applications/Ollama.app/Contents/Resources/ollama"),
    ];
    let Some(binary) = find_binary(&candidates) else {
        log::warn!("services: Ollama is not running and was not found");
        return;
    };
    let (out, err) = quiet_output("ollama.log");
    match Command::new(&binary).arg("serve").stdin(Stdio::null()).stdout(out).stderr(err).spawn() {
        Ok(_) => {
            let ready = wait_until(|| up(http, OLLAMA_URL)).await;
            log::info!("services: started Ollama (ready: {ready})");
        }
        Err(error) => log::warn!("services: cannot start Ollama: {error}"),
    }
}

async fn ensure_api(http: &reqwest::Client, config: &ApiConfig, services: &Services) {
    let api_up = api::health(http, config).await;
    match plan_api_start(api_up, agent_plist().is_file()) {
        ApiStart::AlreadyUp => return,
        ApiStart::Kickstart => {
            // SAFETY: getuid has no preconditions.
            let target = format!("gui/{}/{AGENT_LABEL}", unsafe { libc::getuid() });
            let status = Command::new("/bin/launchctl").args(["kickstart", &target]).status();
            if !matches!(status, Ok(s) if s.success()) {
                // Installed but not loaded: load it, which also starts it.
                let domain = format!("gui/{}", unsafe { libc::getuid() });
                let plist = agent_plist();
                let _ = Command::new("/bin/launchctl")
                    .arg("bootstrap").arg(&domain).arg(&plist).status();
            }
        }
        ApiStart::Spawn => {
            let dir = server_dir_from(&config.data_dir, std::env::var("ASSISTANT_SERVER_DIR").ok().as_deref());
            let python = dir.join(".venv/bin/python");
            if !python.is_file() {
                log::warn!("services: no Python at {}", python.display());
                return;
            }
            let (out, err) = quiet_output("server.log");
            let child = Command::new(&python)
                .args(["-m", "app", "--port", &config.port.to_string()])
                .current_dir(&dir)
                .env("PYTHONUNBUFFERED", "1")
                .stdin(Stdio::null())
                .stdout(out)
                .stderr(err)
                .spawn();
            match child {
                Ok(child) => {
                    if let Ok(mut slot) = services.api_child.lock() {
                        *slot = Some(child);
                    }
                }
                Err(error) => {
                    log::warn!("services: cannot start the assistant server: {error}");
                    return;
                }
            }
        }
    }
    let ready = wait_until(|| api::health(http, config)).await;
    log::info!("services: assistant server ready: {ready}");
}

/// Start whatever is missing. Runs in the background; the UI keeps polling
/// health and fills in once the API answers.
pub async fn ensure_all(http: reqwest::Client, config: ApiConfig, services: &Services) {
    if !managed_from(std::env::var("ASSISTANT_MANAGE_SERVERS").ok().as_deref()) {
        return;
    }
    // Both start in parallel; the API does not need the model to boot.
    tokio::join!(ensure_ollama(&http), ensure_api(&http, &config, services));
}
