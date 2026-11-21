//! Token bridge to the local assistant API.
//!
//! The web view never sees the Bearer token. It asks Rust to make a request
//! (`api_request`), and Rust reads the token file, adds the header, and calls
//! `http://127.0.0.1:<port>` itself. Because the request comes from Rust it has
//! no browser `Origin`, so the server's loopback Origin check stays strict.
//!
//! The token file checks mirror `app/core/auth.py`: a private regular file
//! owned by this user, not a symlink, one link, 43 url-safe characters. The
//! file is read on every request, so a `rotate` is picked up without restart.

use std::fs::File;
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;

pub const TOKEN_NAME: &str = "control_plane.token";
pub const TOKEN_LENGTH: usize = 43;
pub const API_PREFIX: &str = "/api/v1/";
pub const DEFAULT_PORT: u16 = 8000;
const ALLOWED_METHODS: [&str; 5] = ["GET", "POST", "PATCH", "PUT", "DELETE"];

/// Where the server lives and where its token file is.
#[derive(Clone, Debug)]
pub struct ApiConfig {
    pub port: u16,
    pub data_dir: PathBuf,
    /// Validated base URL without a trailing slash (see `resolve_base_url`).
    pub base_url: String,
}

impl ApiConfig {
    /// `ASSISTANT_API_PORT` (default 8000) and `ASSISTANT_DATA_DIR`
    /// (default: the repo's `data/` folder, two levels above this crate).
    pub fn from_env() -> Self {
        let port = std::env::var("ASSISTANT_API_PORT")
            .ok()
            .and_then(|value| value.trim().parse::<u16>().ok())
            .filter(|port| *port > 0)
            .unwrap_or(DEFAULT_PORT);
        let data_dir = std::env::var_os("ASSISTANT_DATA_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join("../../data"));
        let raw = std::env::var("ASSISTANT_API_URL").ok();
        let base_url = match resolve_base_url(raw.as_deref(), port) {
            Ok(url) => url,
            Err(reason) => {
                log::warn!("ignoring ASSISTANT_API_URL: {reason}");
                default_base_url(port)
            }
        };
        Self { port, data_dir, base_url }
    }

    /// Loopback config on `port` (the default).
    pub fn local(port: u16, data_dir: PathBuf) -> Self {
        Self { port, data_dir, base_url: default_base_url(port) }
    }

    pub fn base_url(&self) -> String {
        self.base_url.clone()
    }

    /// True when the API lives on this Mac (loopback host).
    pub fn is_loopback(&self) -> bool {
        split_url(&self.base_url).map(|(_, host, _)| is_loopback_host(&host)).unwrap_or(false)
    }
}

pub fn default_base_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}")
}

pub fn is_loopback_host(host: &str) -> bool {
    matches!(host.to_ascii_lowercase().as_str(), "127.0.0.1" | "::1" | "localhost")
}

/// Split into (scheme, host without brackets, "host:port" authority). Strict:
/// no userinfo, query, fragment, whitespace; path empty or "/".
fn split_url(raw: &str) -> Result<(&'static str, String, String), String> {
    let raw = raw.trim();
    let (scheme, rest) = if let Some(r) = raw.get(..7).filter(|p| p.eq_ignore_ascii_case("http://")).map(|_| &raw[7..]) {
        ("http", r)
    } else if let Some(r) = raw.get(..8).filter(|p| p.eq_ignore_ascii_case("https://")).map(|_| &raw[8..]) {
        ("https", r)
    } else {
        return Err("must start with http:// or https://".into());
    };
    if rest.bytes().any(|b| !b.is_ascii_graphic()) || rest.contains(['@', '?', '#', '\\']) {
        return Err("must not contain userinfo, query, fragment or spaces".into());
    }
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, ""),
    };
    if path != "" && path != "/" {
        return Err("must not have a path".into());
    }
    let (host, port) = if let Some(inner) = authority.strip_prefix('[') {
        let (h, after) = inner.split_once(']').ok_or("bad IPv6 host")?;
        (h, after.strip_prefix(':'))
    } else {
        match authority.rsplit_once(':') {
            Some((h, p)) => (h, Some(p)),
            None => (authority, None),
        }
    };
    if host.is_empty() || host.contains(['[', ']']) {
        return Err("missing host".into());
    }
    if let Some(p) = port {
        if p.parse::<u16>().is_err() {
            return Err("bad port".into());
        }
    }
    Ok((scheme, host.to_ascii_lowercase(), authority.to_ascii_lowercase()))
}

/// Pure rule check for `ASSISTANT_API_URL`. `None` or blank gives the default.
/// `http://` is allowed only for loopback hosts; anything else needs `https://`.
pub fn resolve_base_url(raw: Option<&str>, port: u16) -> Result<String, String> {
    let Some(raw) = raw.map(str::trim).filter(|v| !v.is_empty()) else {
        return Ok(default_base_url(port));
    };
    let (scheme, host, authority) = split_url(raw)?;
    if scheme == "http" && !is_loopback_host(&host) {
        return Err("http:// is only allowed for loopback hosts; use https://".into());
    }
    Ok(format!("{scheme}://{authority}"))
}

/// What the web view gets back. The token is never part of it.
#[derive(Debug, Serialize, PartialEq)]
pub struct ApiResponse {
    pub status: u16,
    pub body: Value,
}

fn valid_token(token: &str) -> bool {
    token.len() == TOKEN_LENGTH
        && token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// Read and check the token file. Errors are short, user-facing sentences.
pub fn read_token(data_dir: &Path) -> Result<String, String> {
    let path = data_dir.join(TOKEN_NAME);
    let mut file = File::options()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(&path)
        .map_err(|error| match error.kind() {
            std::io::ErrorKind::NotFound => {
                "No token yet. Start the assistant server once to create it.".to_string()
            }
            _ => format!("Cannot open the token file: {error}"),
        })?;
    let info = file
        .metadata()
        .map_err(|error| format!("Cannot check the token file: {error}"))?;
    // SAFETY: getuid has no preconditions and cannot fail.
    let uid = unsafe { libc::getuid() };
    if !info.file_type().is_file() || info.uid() != uid || info.nlink() != 1 {
        return Err("The token file is not a private regular file.".into());
    }
    if info.mode() & 0o077 != 0 {
        return Err("The token file is readable by other users. Run chmod 600 on it.".into());
    }
    let mut raw = Vec::with_capacity(TOKEN_LENGTH + 1);
    file.by_ref()
        .take((TOKEN_LENGTH + 1) as u64)
        .read_to_end(&mut raw)
        .map_err(|error| format!("Cannot read the token file: {error}"))?;
    let token = String::from_utf8(raw).unwrap_or_default();
    if !valid_token(&token) {
        return Err("The token file is damaged. Run: python -m app.control_plane rotate".into());
    }
    Ok(token)
}

/// Only API paths under `/api/v1/`, with an optional query string.
/// Rejects anything that could change the host or escape the prefix.
pub fn check_path(path: &str) -> Result<(), String> {
    let (route, _query) = path.split_once('?').unwrap_or((path, ""));
    let bad = !path.starts_with(API_PREFIX)
        || path.len() > 2048
        || path.bytes().any(|b| !b.is_ascii_graphic())
        || path.contains('\\')
        || path.contains('#')
        || route.contains("//")
        || route.split('/').any(|part| part == "." || part == "..")
        || route.to_ascii_lowercase().contains("%2e")
        || route.to_ascii_lowercase().contains("%2f");
    if bad {
        return Err(format!("Refused API path: {path}"));
    }
    Ok(())
}

pub fn check_method(method: &str) -> Result<reqwest::Method, String> {
    let upper = method.to_ascii_uppercase();
    if !ALLOWED_METHODS.contains(&upper.as_str()) {
        return Err(format!("Refused HTTP method: {method}"));
    }
    reqwest::Method::from_bytes(upper.as_bytes()).map_err(|_| format!("Refused HTTP method: {method}"))
}

pub fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .no_proxy() // never send the token through a system proxy
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(600)) // a local model can be slow
        .build()
        .map_err(|error| format!("Cannot create the HTTP client: {error}"))
}

fn parse_body(bytes: &[u8]) -> Value {
    if bytes.is_empty() {
        return Value::Null;
    }
    serde_json::from_slice(bytes)
        .unwrap_or_else(|_| Value::String(String::from_utf8_lossy(bytes).into_owned()))
}

/// Send one authenticated request. HTTP errors come back as a status, not an Err;
/// Err means the request never reached the server or was refused here.
pub async fn send(
    http: &reqwest::Client,
    config: &ApiConfig,
    method: &str,
    path: &str,
    body: Option<Value>,
) -> Result<ApiResponse, String> {
    let method = check_method(method)?;
    check_path(path)?;
    // Defence in depth: never put the token on plain http off this Mac.
    if config.base_url.to_ascii_lowercase().starts_with("http://") && !config.is_loopback() {
        return Err("Refused: the API address must use https:// unless it is on this Mac.".into());
    }
    let token = read_token(&config.data_dir)?;
    let mut request = http
        .request(method, format!("{}{}", config.base_url(), path))
        .bearer_auth(token);
    if let Some(body) = body {
        request = request.json(&body);
    }
    let response = request.send().await.map_err(|error| {
        if error.is_connect() {
            if config.is_loopback() {
                format!(
                    "The assistant server is not running on port {}. The app starts it on launch; try again in a few seconds.",
                    config.port
                )
            } else {
                format!("Cannot reach the assistant server at {}.", config.base_url)
            }
        } else if error.is_timeout() {
            "The assistant took too long to answer.".to_string()
        } else {
            format!("Request failed: {error}")
        }
    })?;
    let status = response.status().as_u16();
    let bytes = response
        .bytes()
        .await
        .map_err(|error| format!("Cannot read the answer: {error}"))?;
    Ok(ApiResponse { status, body: parse_body(&bytes) })
}

/// Public health check (no token needed). True when the server answers 200.
pub async fn health(http: &reqwest::Client, config: &ApiConfig) -> bool {
    let url = format!("{}{}health", config.base_url(), API_PREFIX);
    matches!(http.get(url).timeout(Duration::from_secs(3)).send().await, Ok(r) if r.status().is_success())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(v: &str) -> String { resolve_base_url(Some(v), 8000).unwrap() }
    fn bad(v: &str) { assert!(resolve_base_url(Some(v), 8000).is_err(), "{v}"); }

    #[test]
    fn default_when_unset_or_blank() {
        assert_eq!(resolve_base_url(None, 9001).unwrap(), "http://127.0.0.1:9001");
        assert_eq!(resolve_base_url(Some("  "), 8000).unwrap(), "http://127.0.0.1:8000");
    }

    #[test]
    fn http_only_for_loopback() {
        assert_eq!(ok("http://127.0.0.1:8123/"), "http://127.0.0.1:8123");
        assert_eq!(ok("http://localhost:8000"), "http://localhost:8000");
        assert_eq!(ok("http://[::1]:8000"), "http://[::1]:8000");
        bad("http://example.com");
        bad("http://192.168.1.5:8000");
        bad("http://localhost.evil.com");
    }

    #[test]
    fn https_anywhere() {
        assert_eq!(ok("https://api.example.com/"), "https://api.example.com");
        assert_eq!(ok("HTTPS://Api.Example.com:8443"), "https://api.example.com:8443");
    }

    #[test]
    fn rejects_userinfo_query_fragment_path_and_junk() {
        bad("https://user:pw@example.com");
        bad("https://example.com?x=1");
        bad("https://example.com/#f");
        bad("https://example.com/api");
        bad("https://example.com:99999");
        bad("https://");
        bad("ftp://example.com");
        bad("example.com");
        bad("https://exa mple.com");
    }

    #[test]
    fn is_loopback_follows_url() {
        let dir: PathBuf = "/tmp".into();
        assert!(ApiConfig::local(8000, dir.clone()).is_loopback());
        let mut c = ApiConfig::local(8000, dir.clone());
        c.base_url = "https://api.example.com".into();
        assert!(!c.is_loopback());
        c.base_url = "http://localhost:1".into();
        assert!(c.is_loopback());
    }
}
