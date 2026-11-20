//! Live check against a running assistant server, through the same bridge the app uses.
//! Ignored by default. Run with a server up (and Ollama for the chat step):
//!
//!   ASSISTANT_API_PORT=8765 cargo test --test live -- --ignored --nocapture
//!
//! It sends one real chat and then deletes that conversation.

use assistant_desktop_lib::api::{self, ApiConfig};
use serde_json::json;

#[tokio::test]
#[ignore = "needs a running server"]
async fn chat_round_trip_through_the_bridge() {
    let config = ApiConfig::from_env();
    let http = api::client().unwrap();
    assert!(api::health(&http, &config).await, "server not up on port {}", config.port);

    let list = api::send(&http, &config, "GET", "/api/v1/conversations?limit=5", None).await.unwrap();
    assert_eq!(list.status, 200, "{:?}", list.body);

    let reply = api::send(
        &http,
        &config,
        "POST",
        "/api/v1/chat",
        Some(json!({ "message": "رد بكلمة واحدة: تمام" })),
    )
    .await
    .unwrap();
    assert_eq!(reply.status, 200, "{:?}", reply.body);
    let answer = reply.body["answer"].as_str().unwrap_or_default();
    let id = reply.body["conversation_id"].as_str().expect("conversation id").to_string();
    println!("model={} answer={answer}", reply.body["model"]);
    assert!(!answer.trim().is_empty());

    let history = api::send(&http, &config, "GET", &format!("/api/v1/conversations/{id}/messages?limit=200"), None)
        .await
        .unwrap();
    assert_eq!(history.status, 200);
    assert!(history.body["count"].as_u64().unwrap() >= 2);

    let deleted = api::send(&http, &config, "DELETE", &format!("/api/v1/conversations/{id}"), None).await.unwrap();
    assert_eq!(deleted.status, 204, "{:?}", deleted.body);
}
