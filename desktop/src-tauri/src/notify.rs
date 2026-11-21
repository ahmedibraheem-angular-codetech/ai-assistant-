//! Desktop notifications for urgent insights.
//!
//! Every 5 minutes Rust asks `GET /api/v1/insights` through the same token
//! bridge as the web view, and shows a macOS notification for each high-severity
//! insight it has not shown yet in this run. Ids are remembered in memory only,
//! so the first poll after launch notifies the high items that exist then.

use std::collections::HashSet;
use std::time::Duration;

use serde_json::Value;
use tauri::AppHandle;
use tauri_plugin_notification::NotificationExt;

use crate::api::{self, ApiConfig};

const EVERY: Duration = Duration::from_secs(300);
const FIRST_DELAY: Duration = Duration::from_secs(20);
const INSIGHTS_PATH: &str = "/api/v1/insights";

/// High-severity insights whose id is not in `seen` yet, as (title, detail).
/// Their ids are added to `seen`, so each is returned once.
pub fn new_high(body: &Value, seen: &mut HashSet<String>) -> Vec<(String, String)> {
    let Some(items) = body.get("insights").and_then(Value::as_array) else {
        return Vec::new();
    };
    let mut fresh = Vec::new();
    for item in items {
        let text = |key: &str| item.get(key).and_then(Value::as_str);
        if text("severity") != Some("high") {
            continue;
        }
        let Some(id) = text("id") else { continue };
        if seen.insert(id.to_string()) {
            fresh.push((text("title").unwrap_or("تنبيه").to_string(), text("detail").unwrap_or("").to_string()));
        }
    }
    fresh
}

/// Poll forever. Failures are logged and retried at the next tick.
pub async fn run(app: AppHandle, http: reqwest::Client, config: ApiConfig) {
    let _ = app.notification().request_permission();
    tokio::time::sleep(FIRST_DELAY).await;
    let mut seen = HashSet::new();
    loop {
        match api::send(&http, &config, "GET", INSIGHTS_PATH, None).await {
            Ok(response) if response.status == 200 => {
                for (title, body) in new_high(&response.body, &mut seen) {
                    if let Err(error) = app.notification().builder().title(title).body(body).show() {
                        log::warn!("notification failed: {error}");
                    }
                }
            }
            Ok(response) => log::warn!("insights poll returned {}", response.status),
            Err(error) => log::warn!("insights poll failed: {error}"),
        }
        tokio::time::sleep(EVERY).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn body() -> Value {
        json!({"insights": [
            {"id": "a", "severity": "high", "title": "A", "detail": "da"},
            {"id": "b", "severity": "warn", "title": "B", "detail": "db"},
            {"id": "c", "severity": "info", "title": "C", "detail": "dc"},
        ]})
    }

    #[test]
    fn only_high_items_are_new_and_each_is_returned_once() {
        let mut seen = HashSet::new();
        assert_eq!(new_high(&body(), &mut seen), vec![("A".to_string(), "da".to_string())]);
        assert!(new_high(&body(), &mut seen).is_empty());
    }

    #[test]
    fn a_later_high_item_is_new_and_bad_bodies_are_ignored() {
        let mut seen = HashSet::new();
        new_high(&body(), &mut seen);
        let later = json!({"insights": [
            {"id": "a", "severity": "high", "title": "A", "detail": "da"},
            {"id": "d", "severity": "high", "title": "D", "detail": "dd"},
            {"severity": "high"},
        ]});
        assert_eq!(new_high(&later, &mut seen), vec![("D".to_string(), "dd".to_string())]);
        assert!(new_high(&json!({"nope": 1}), &mut seen).is_empty());
    }
}
