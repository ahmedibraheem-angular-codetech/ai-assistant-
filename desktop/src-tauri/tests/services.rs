//! The app starts the servers it needs when they are not running.

use std::fs;
use std::path::PathBuf;

use assistant_desktop_lib::services::{
    find_binary, managed_from, plan_api_start, server_dir_from, ApiStart,
};

#[test]
fn api_already_running_is_left_alone() {
    assert_eq!(plan_api_start(true, true), ApiStart::AlreadyUp);
    assert_eq!(plan_api_start(true, false), ApiStart::AlreadyUp);
}

#[test]
fn launch_agent_is_preferred_over_a_child_process() {
    // One server at a time: if the login item exists, ask launchd to start it
    // instead of starting a second copy that would lose the serve lease.
    assert_eq!(plan_api_start(false, true), ApiStart::Kickstart);
    assert_eq!(plan_api_start(false, false), ApiStart::Spawn);
}

#[test]
fn servers_are_managed_unless_turned_off() {
    assert!(managed_from(None));
    assert!(managed_from(Some("1")));
    assert!(!managed_from(Some("0")));
    assert!(!managed_from(Some(" off ")));
    assert!(!managed_from(Some("false")));
}

#[test]
fn server_dir_is_the_parent_of_data_unless_overridden() {
    let data = PathBuf::from("/repo/data");
    assert_eq!(server_dir_from(&data, None), PathBuf::from("/repo"));
    assert_eq!(server_dir_from(&data, Some("/other")), PathBuf::from("/other"));
    assert_eq!(server_dir_from(&data, Some("  ")), PathBuf::from("/repo"));
}

#[test]
fn find_binary_returns_the_first_existing_executable() {
    let dir = tempfile::tempdir().unwrap();
    let missing = dir.path().join("nope/ollama");
    let plain = dir.path().join("plain");
    fs::write(&plain, "").unwrap(); // not executable
    let exe = dir.path().join("ollama");
    fs::write(&exe, "#!/bin/sh\n").unwrap();
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(&exe, fs::Permissions::from_mode(0o755)).unwrap();

    assert_eq!(find_binary(&[missing.clone(), plain.clone(), exe.clone()]), Some(exe));
    assert_eq!(find_binary(&[missing, plain]), None);
}
