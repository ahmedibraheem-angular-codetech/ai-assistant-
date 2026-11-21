use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::thread;

use assistant_desktop_lib::api::{self, ApiConfig, TOKEN_NAME};
use serde_json::json;

const TOKEN: &str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ0123-_Z"; // 43 chars

fn write_token(dir: &Path, token: &str, mode: u32) {
    let path = dir.join(TOKEN_NAME);
    fs::write(&path, token).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
}

// ---------------------------------------------------------------- token file

#[test]
fn reads_a_private_token() {
    assert_eq!(TOKEN.len(), 43);
    let dir = tempfile::tempdir().unwrap();
    write_token(dir.path(), TOKEN, 0o600);
    assert_eq!(api::read_token(dir.path()).unwrap(), TOKEN);
}

#[test]
fn missing_token_says_start_the_server() {
    let dir = tempfile::tempdir().unwrap();
    let error = api::read_token(dir.path()).unwrap_err();
    assert!(error.contains("Start the assistant server"), "{error}");
}

#[test]
fn refuses_a_token_readable_by_others() {
    let dir = tempfile::tempdir().unwrap();
    write_token(dir.path(), TOKEN, 0o644);
    assert!(api::read_token(dir.path()).unwrap_err().contains("chmod 600"));
}

#[test]
fn refuses_a_damaged_token() {
    let dir = tempfile::tempdir().unwrap();
    write_token(dir.path(), "short", 0o600);
    assert!(api::read_token(dir.path()).unwrap_err().contains("damaged"));
    write_token(dir.path(), &format!("{TOKEN}x"), 0o600);
    assert!(api::read_token(dir.path()).unwrap_err().contains("damaged"));
    write_token(dir.path(), &format!("{}!", &TOKEN[..42]), 0o600);
    assert!(api::read_token(dir.path()).unwrap_err().contains("damaged"));
}

#[test]
fn refuses_a_symlinked_token() {
    let dir = tempfile::tempdir().unwrap();
    let real = dir.path().join("elsewhere");
    fs::write(&real, TOKEN).unwrap();
    fs::set_permissions(&real, fs::Permissions::from_mode(0o600)).unwrap();
    std::os::unix::fs::symlink(&real, dir.path().join(TOKEN_NAME)).unwrap();
    assert!(api::read_token(dir.path()).is_err());
}

#[test]
fn refuses_a_hard_linked_token() {
    let dir = tempfile::tempdir().unwrap();
    write_token(dir.path(), TOKEN, 0o600);
    fs::hard_link(dir.path().join(TOKEN_NAME), dir.path().join("copy")).unwrap();
    assert!(api::read_token(dir.path()).unwrap_err().contains("private regular file"));
}

// ---------------------------------------------------------------- guards

#[test]
fn accepts_api_paths() {
    for path in [
        "/api/v1/chat",
        "/api/v1/conversations?limit=20&offset=0",
        "/api/v1/conversations/1b2c/messages?limit=200",
    ] {
        api::check_path(path).unwrap();
    }
}

#[test]
fn refuses_paths_that_leave_the_api() {
    for path in [
        "/health",
        "api/v1/chat",
        "http://evil.example/api/v1/chat",
        "//evil.example/api/v1/chat",
        "/api/v1/../admin",
        "/api/v1/%2e%2e/admin",
        "/api/v1/a%2Fb",
        "/api/v1//chat",
        "/api/v1/chat#x",
        "/api/v1/chat\\x",
        "/api/v1/chat x",
        "/api/v1/chat\n",
        "@evil.example/api/v1/",
    ] {
        assert!(api::check_path(path).is_err(), "should refuse {path:?}");
    }
}

#[test]
fn only_known_methods() {
    for method in ["GET", "post", "PATCH", "PUT", "DELETE"] {
        api::check_method(method).unwrap();
    }
    for method in ["CONNECT", "TRACE", "OPTIONS", "G ET", ""] {
        assert!(api::check_method(method).is_err(), "should refuse {method:?}");
    }
}

#[test]
fn base_url_is_always_loopback() {
    let config = ApiConfig::local(8765, "/tmp".into());
    assert_eq!(config.base_url(), "http://127.0.0.1:8765");
}

// ---------------------------------------------------------------- live request

/// One-shot HTTP server: answers once and returns the raw request it received.
fn serve_once(status_line: &'static str, body: &'static str) -> (u16, thread::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut buffer = vec![0u8; 16384];
        let mut received = Vec::new();
        loop {
            let read = stream.read(&mut buffer).unwrap();
            received.extend_from_slice(&buffer[..read]);
            if read == 0 {
                break;
            }
            let text = String::from_utf8_lossy(&received).into_owned();
            if let Some(end) = text.find("\r\n\r\n") {
                let length = text[..end]
                    .lines()
                    .find_map(|line| {
                        line.to_ascii_lowercase()
                            .strip_prefix("content-length:")
                            .map(|v| v.trim().parse::<usize>().unwrap())
                    })
                    .unwrap_or(0);
                if received.len() >= end + 4 + length {
                    break;
                }
            }
        }
        let response = format!(
            "{status_line}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(response.as_bytes()).unwrap();
        String::from_utf8_lossy(&received).into_owned()
    });
    (port, handle)
}

#[tokio::test]
async fn sends_bearer_token_and_no_origin() {
    let dir = tempfile::tempdir().unwrap();
    write_token(dir.path(), TOKEN, 0o600);
    let (port, server) = serve_once("HTTP/1.1 200 OK", r#"{"answer":"أهلاً"}"#);
    let config = ApiConfig::local(port, dir.path().into());

    let response = api::send(
        &api::client().unwrap(),
        &config,
        "POST",
        "/api/v1/chat",
        Some(json!({"message": "مرحبا"})),
    )
    .await
    .unwrap();

    assert_eq!(response.status, 200);
    assert_eq!(response.body, json!({"answer": "أهلاً"}));
    let request = server.join().unwrap();
    let lower = request.to_ascii_lowercase();
    assert!(request.starts_with("POST /api/v1/chat HTTP/1.1"), "{request}");
    assert!(lower.contains(&format!("authorization: bearer {}", TOKEN.to_ascii_lowercase())), "{request}");
    assert!(lower.contains(&format!("host: 127.0.0.1:{port}")), "{request}");
    assert!(!lower.contains("\norigin:"), "bridge must not send an Origin header");
    assert!(request.contains("مرحبا"));
}

#[tokio::test]
async fn http_errors_come_back_as_status() {
    let dir = tempfile::tempdir().unwrap();
    write_token(dir.path(), TOKEN, 0o600);
    let (port, server) = serve_once("HTTP/1.1 423 Locked", r#"{"detail":"Paused"}"#);
    let config = ApiConfig::local(port, dir.path().into());
    let response = api::send(&api::client().unwrap(), &config, "GET", "/api/v1/conversations", None)
        .await
        .unwrap();
    server.join().unwrap();
    assert_eq!(response.status, 423);
    assert_eq!(response.body, json!({"detail": "Paused"}));
}

#[tokio::test]
async fn refused_path_never_reads_token_or_connects() {
    let dir = tempfile::tempdir().unwrap(); // no token file on purpose
    let config = ApiConfig::local(1, dir.path().into());
    let error = api::send(&api::client().unwrap(), &config, "GET", "/api/v1/../x", None)
        .await
        .unwrap_err();
    assert!(error.starts_with("Refused API path"), "{error}");
}

#[tokio::test]
async fn server_down_gives_a_clear_message() {
    let dir = tempfile::tempdir().unwrap();
    write_token(dir.path(), TOKEN, 0o600);
    let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port(); // closed again
    let config = ApiConfig::local(port, dir.path().into());
    let error = api::send(&api::client().unwrap(), &config, "GET", "/api/v1/conversations", None)
        .await
        .unwrap_err();
    assert!(error.contains("not running"), "{error}");
}
