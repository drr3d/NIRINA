#![allow(dead_code)]
use std::sync::{Arc, Mutex};

use axum::{
    Json, Router,
    body::Body,
    extract::State,
    http::{HeaderMap, Method, Request, StatusCode},
    routing::post,
};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use tower::ServiceExt;

pub type Rekam = Arc<Mutex<Vec<(Option<String>, Value)>>>;

pub async fn jalankan_upstream(router: Router) -> String {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(l, router).await.unwrap();
    });
    format!("http://{addr}/v1")
}

pub fn upstream_normal(rekam: Rekam) -> Router {
    Router::new()
        .route(
            "/v1/chat/completions",
            post(|State(r): State<Rekam>, h: HeaderMap, Json(b): Json<Value>| async move {
                let auth = h.get("authorization").and_then(|v| v.to_str().ok()).map(String::from);
                r.lock().unwrap().push((auth, b));
                Json(json!({
                    "id": "x",
                    "choices": [{"message": {"role": "assistant", "content": null, "tool_calls": [{"id": "c1"}]}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
                }))
            }),
        )
        .with_state(rekam)
}

pub async fn kirim(router: Router, metode: Method, uri: &str, body: Option<&str>) -> (StatusCode, String) {
    let req = Request::builder()
        .method(metode)
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.unwrap_or("").to_string()))
        .unwrap();
    let resp = router.oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).to_string())
}

pub fn kode_error(teks: &str) -> String {
    let v: Value = serde_json::from_str(teks).unwrap();
    v["error"]["code"].as_str().unwrap().to_string()
}
