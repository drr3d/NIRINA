use std::sync::Arc;

use axum::{
    Json, Router,
    body::Body,
    http::{Method, Request, StatusCode},
    routing::post,
};
use http_body_util::BodyExt;
use nigate::{AppState, app, config::Config, keys::KeyStore};
use serde_json::{Value, json};
use tower::ServiceExt;

mod common;
use common::*;

const CHAT: &str = r#"{"model":"m1","messages":[{"role":"user","content":"hi"}]}"#;

fn buat(base: &str, tambahan_config: &str) -> (Router, Arc<KeyStore>) {
    let teks = format!("{tambahan_config}\n[[model]]\nalias = \"m1\"\n[[model.upstream]]\nbase_url = \"{base}\"\nmodel = \"asli\"\n");
    let cfg = Config::from_toml_str(&teks, &|_| None).unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    (app(AppState::new(cfg, keys.clone()).unwrap()), keys)
}

struct Jawaban {
    status: StatusCode,
    retry_after: Option<String>,
    body: String,
}

async fn chat(r: &Router, token: &str, body: &str) -> Jawaban {
    let req = Request::builder()
        .method(Method::POST)
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .header("authorization", format!("Bearer {token}"))
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = r.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let retry_after = resp.headers().get("retry-after").map(|v| v.to_str().unwrap().to_string());
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    Jawaban { status, retry_after, body: String::from_utf8_lossy(&bytes).to_string() }
}

fn upstream_usage(total: u64) -> Router {
    Router::new().route("/v1/chat/completions", post(move || async move { Json(json!({"choices": [], "usage": {"total_tokens": total}})) }))
}

fn upstream_500() -> Router {
    Router::new().route("/v1/chat/completions", post(|| async { (StatusCode::INTERNAL_SERVER_ERROR, "rusak") }))
}

#[tokio::test]
async fn rpm_terlampaui_429_dengan_retry_after_dan_bentuk_error_openai() {
    let base = jalankan_upstream(upstream_normal(Arc::default())).await;
    let (r, keys) = buat(&base, "");
    let (_, token) = keys.create("a").unwrap();
    keys.set_limits("a", Some(1), None).unwrap();

    assert_eq!(chat(&r, &token, CHAT).await.status, StatusCode::OK);
    let j = chat(&r, &token, CHAT).await;
    assert_eq!(j.status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(kode_error(&j.body), "rate_limit_exceeded");
    let v: Value = serde_json::from_str(&j.body).unwrap();
    assert_eq!(v["error"]["type"], "rate_limit_error");
    assert!(v["error"]["message"].as_str().unwrap().contains("RPM"));
    let ra: u64 = j.retry_after.expect("harus ada Retry-After").parse().unwrap();
    assert!((1..=60).contains(&ra));
}

#[tokio::test]
async fn key_lain_dan_key_tanpa_batas_tidak_terpengaruh() {
    let base = jalankan_upstream(upstream_normal(Arc::default())).await;
    let (r, keys) = buat(&base, "");
    let (_, terbatas) = keys.create("terbatas").unwrap();
    let (_, bebas) = keys.create("bebas").unwrap();
    let (_, lain) = keys.create("lain").unwrap();
    keys.set_limits("terbatas", Some(1), None).unwrap();
    keys.set_limits("lain", Some(1), None).unwrap();

    assert_eq!(chat(&r, &terbatas, CHAT).await.status, StatusCode::OK);
    assert_eq!(chat(&r, &terbatas, CHAT).await.status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(chat(&r, &lain, CHAT).await.status, StatusCode::OK);
    for _ in 0..5 {
        assert_eq!(chat(&r, &bebas, CHAT).await.status, StatusCode::OK);
    }
}

#[tokio::test]
async fn default_dari_config_berlaku_dan_batas_key_menggantikannya() {
    let base = jalankan_upstream(upstream_normal(Arc::default())).await;
    let (r, keys) = buat(&base, "[limits]\ndefault_rpm = 1");
    let (_, biasa) = keys.create("biasa").unwrap();
    let (_, vip) = keys.create("vip").unwrap();
    keys.set_limits("vip", Some(3), None).unwrap();

    assert_eq!(chat(&r, &biasa, CHAT).await.status, StatusCode::OK);
    assert_eq!(chat(&r, &biasa, CHAT).await.status, StatusCode::TOO_MANY_REQUESTS);
    for _ in 0..3 {
        assert_eq!(chat(&r, &vip, CHAT).await.status, StatusCode::OK);
    }
    assert_eq!(chat(&r, &vip, CHAT).await.status, StatusCode::TOO_MANY_REQUESTS);
}

#[tokio::test]
async fn tpm_dikoreksi_dengan_usage_asli_lalu_request_berikutnya_tertahan() {
    let base = jalankan_upstream(upstream_usage(5000)).await;
    let (r, keys) = buat(&base, "");
    let (_, token) = keys.create("a").unwrap();
    keys.set_limits("a", None, Some(1000)).unwrap();

    assert_eq!(chat(&r, &token, CHAT).await.status, StatusCode::OK, "estimasi kecil, lolos");
    let j = chat(&r, &token, CHAT).await;
    assert_eq!(j.status, StatusCode::TOO_MANY_REQUESTS, "usage asli 5000 sudah melewati 1000 TPM");
    assert!(j.body.contains("TPM"));
    assert!(j.retry_after.unwrap().parse::<u64>().unwrap() > 60);
}

#[tokio::test]
async fn upstream_gagal_mengembalikan_jatah_tpm() {
    let base = jalankan_upstream(upstream_500()).await;
    let (r, keys) = buat(&base, "");
    let (_, token) = keys.create("a").unwrap();
    keys.set_limits("a", None, Some(100)).unwrap();

    let besar = format!(r#"{{"model":"m1","messages":[{{"role":"user","content":"{}"}}]}}"#, "x".repeat(300)); // ~80 token
    for i in 0..5 {
        assert_eq!(chat(&r, &token, &besar).await.status, StatusCode::INTERNAL_SERVER_ERROR, "panggilan ke-{i} tidak boleh kena TPM");
    }
}

#[tokio::test]
async fn upstream_mati_mengembalikan_jatah_tpm_tetapi_rpm_tetap_terpakai() {
    let (r, keys) = buat("http://127.0.0.1:1/v1", "");
    let (_, token) = keys.create("a").unwrap();
    keys.set_limits("a", Some(2), Some(100)).unwrap();
    let besar = format!(r#"{{"model":"m1","messages":[{{"role":"user","content":"{}"}}]}}"#, "x".repeat(300));

    assert_eq!(chat(&r, &token, &besar).await.status, StatusCode::BAD_GATEWAY);
    assert_eq!(chat(&r, &token, &besar).await.status, StatusCode::BAD_GATEWAY, "TPM dikembalikan");
    assert_eq!(chat(&r, &token, &besar).await.status, StatusCode::TOO_MANY_REQUESTS, "RPM 2 sudah terpakai");
}

#[tokio::test]
async fn request_tidak_valid_tidak_memakai_jatah() {
    let base = jalankan_upstream(upstream_normal(Arc::default())).await;
    let (r, keys) = buat(&base, "");
    let (_, token) = keys.create("a").unwrap();
    keys.set_limits("a", Some(1), None).unwrap();

    assert_eq!(chat(&r, &token, "bukan json").await.status, StatusCode::BAD_REQUEST);
    assert_eq!(chat(&r, &token, r#"{"model":"tidak-ada"}"#).await.status, StatusCode::NOT_FOUND);
    assert_eq!(chat(&r, &token, CHAT).await.status, StatusCode::OK, "jatah RPM masih utuh");
}

#[tokio::test]
async fn perubahan_batas_langsung_berlaku_tanpa_restart() {
    let base = jalankan_upstream(upstream_normal(Arc::default())).await;
    let (r, keys) = buat(&base, "");
    let (_, token) = keys.create("a").unwrap();
    for _ in 0..3 {
        assert_eq!(chat(&r, &token, CHAT).await.status, StatusCode::OK);
    }
    keys.set_limits("a", Some(1), None).unwrap();
    assert_eq!(chat(&r, &token, CHAT).await.status, StatusCode::OK);
    assert_eq!(chat(&r, &token, CHAT).await.status, StatusCode::TOO_MANY_REQUESTS);
    keys.set_limits("a", None, None).unwrap();
    assert_eq!(chat(&r, &token, CHAT).await.status, StatusCode::OK);
}

#[test]
fn config_menolak_default_nol() {
    assert!(Config::from_toml_str("[limits]\ndefault_rpm = 0\n", &|_| None).is_err());
    assert!(Config::from_toml_str("[limits]\ndefault_tpm = 0\n", &|_| None).is_err());
}
