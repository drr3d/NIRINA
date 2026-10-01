use std::sync::Arc;
use std::time::Duration;

use axum::{
    Json, Router,
    http::{Method, StatusCode},
    routing::post,
};
use nigate::{AppState, app, config::Config, keys::KeyStore};
use serde_json::{Value, json};

mod common;
use common::*;

fn upstream_429() -> Router {
    Router::new().route(
        "/v1/chat/completions",
        post(|| async { (StatusCode::TOO_MANY_REQUESTS, Json(json!({"error": {"message": "limit provider"}}))) }),
    )
}

fn upstream_lambat() -> Router {
    Router::new().route(
        "/v1/chat/completions",
        post(|| async {
            tokio::time::sleep(Duration::from_secs(4)).await;
            Json(json!({"ok": true}))
        }),
    )
}

fn buat_app(base: &str, pakai_key_env: bool, nilai_key: Option<&str>, timeout: u64) -> Router {
    let key_baris = if pakai_key_env { "api_key_env = \"K\"" } else { "" };
    let teks = format!(
        "[auth]\nrequired = false\n[[model]]\nalias = \"m1\"\n[[model.upstream]]\nbase_url = \"{base}\"\nmodel = \"asli\"\n{key_baris}\ntimeout_secs = {timeout}\n"
    );
    let nilai = nilai_key.map(String::from);
    let cfg = Config::from_toml_str(&teks, &move |n| if n == "K" { nilai.clone() } else { None }).unwrap();
    app(AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap())).unwrap())
}

#[tokio::test]
async fn healthz_ok() {
    let r = buat_app("http://127.0.0.1:1/v1", false, None, 5);
    let (s, b) = kirim(r, Method::GET, "/healthz", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(serde_json::from_str::<Value>(&b).unwrap()["status"], "ok");
}

#[tokio::test]
async fn models_menampilkan_alias() {
    let r = buat_app("http://127.0.0.1:1/v1", false, None, 5);
    let (s, b) = kirim(r, Method::GET, "/v1/models", None).await;
    assert_eq!(s, StatusCode::OK);
    let v: Value = serde_json::from_str(&b).unwrap();
    assert_eq!(v["object"], "list");
    assert_eq!(v["data"][0]["id"], "m1");
}

#[tokio::test]
async fn chat_meneruskan_dan_mengganti_model_serta_key() {
    let rekam: Rekam = Arc::default();
    let base = jalankan_upstream(upstream_normal(rekam.clone())).await;
    let r = buat_app(&base, true, Some("rahasia-123"), 5);
    let body = r#"{"model":"m1","messages":[{"role":"user","content":"halo"}],"temperature":0.2,"tools":[{"type":"function"}]}"#;
    let (s, b) = kirim(r, Method::POST, "/v1/chat/completions", Some(body)).await;
    assert_eq!(s, StatusCode::OK);
    let v: Value = serde_json::from_str(&b).unwrap();
    assert_eq!(v["choices"][0]["message"]["tool_calls"][0]["id"], "c1");
    assert_eq!(v["usage"]["total_tokens"], 5);

    let diterima = rekam.lock().unwrap();
    assert_eq!(diterima.len(), 1);
    assert_eq!(diterima[0].0.as_deref(), Some("Bearer rahasia-123"));
    assert_eq!(diterima[0].1["model"], "asli");
    assert_eq!(diterima[0].1["temperature"], 0.2);
    assert_eq!(diterima[0].1["messages"][0]["content"], "halo");
    assert!(diterima[0].1["tools"].is_array());
}

#[tokio::test]
async fn upstream_tanpa_key_tidak_mengirim_authorization() {
    let rekam: Rekam = Arc::default();
    let base = jalankan_upstream(upstream_normal(rekam.clone())).await;
    let r = buat_app(&base, false, None, 5);
    let (s, _) = kirim(r, Method::POST, "/v1/chat/completions", Some(r#"{"model":"m1","messages":[]}"#)).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(rekam.lock().unwrap()[0].0, None);
}

#[tokio::test]
async fn alias_tidak_dikenal_404() {
    let r = buat_app("http://127.0.0.1:1/v1", false, None, 5);
    let (s, b) = kirim(r, Method::POST, "/v1/chat/completions", Some(r#"{"model":"tidak-ada","messages":[]}"#)).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(kode_error(&b), "model_not_found");
}

#[tokio::test]
async fn request_tidak_valid_400() {
    let r = || buat_app("http://127.0.0.1:1/v1", false, None, 5);
    let (s, b) = kirim(r(), Method::POST, "/v1/chat/completions", Some("bukan json")).await;
    assert_eq!((s, kode_error(&b).as_str()), (StatusCode::BAD_REQUEST, "invalid_json"));
    let (s, b) = kirim(r(), Method::POST, "/v1/chat/completions", Some("[1,2]")).await;
    assert_eq!((s, kode_error(&b).as_str()), (StatusCode::BAD_REQUEST, "invalid_body"));
    let (s, b) = kirim(r(), Method::POST, "/v1/chat/completions", Some(r#"{"messages":[]}"#)).await;
    assert_eq!((s, kode_error(&b).as_str()), (StatusCode::BAD_REQUEST, "missing_model"));
}

#[tokio::test]
async fn stream_true_ditolak_jelas() {
    let r = buat_app("http://127.0.0.1:1/v1", false, None, 5);
    let (s, b) = kirim(r, Method::POST, "/v1/chat/completions", Some(r#"{"model":"m1","stream":true,"messages":[]}"#)).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(kode_error(&b), "stream_unsupported");
}

#[tokio::test]
async fn env_key_kosong_503_tanpa_membocorkan_detail() {
    let r = buat_app("http://127.0.0.1:1/v1", true, None, 5);
    let (s, b) = kirim(r, Method::POST, "/v1/chat/completions", Some(r#"{"model":"m1","messages":[]}"#)).await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(kode_error(&b), "upstream_not_configured");
    assert!(!b.contains("env") && !b.contains("\"K\""));
}

#[tokio::test]
async fn status_upstream_diteruskan_apa_adanya() {
    let base = jalankan_upstream(upstream_429()).await;
    let r = buat_app(&base, false, None, 5);
    let (s, b) = kirim(r, Method::POST, "/v1/chat/completions", Some(r#"{"model":"m1","messages":[]}"#)).await;
    assert_eq!(s, StatusCode::TOO_MANY_REQUESTS);
    assert!(b.contains("limit provider"));
}

#[tokio::test]
async fn upstream_mati_502() {
    let r = buat_app("http://127.0.0.1:1/v1", false, None, 5);
    let (s, b) = kirim(r, Method::POST, "/v1/chat/completions", Some(r#"{"model":"m1","messages":[]}"#)).await;
    assert_eq!(s, StatusCode::BAD_GATEWAY);
    assert_eq!(kode_error(&b), "upstream_unreachable");
    assert!(!b.contains("127.0.0.1"));
}

#[tokio::test]
async fn upstream_lambat_504() {
    let base = jalankan_upstream(upstream_lambat()).await;
    let r = buat_app(&base, false, None, 1);
    let (s, b) = kirim(r, Method::POST, "/v1/chat/completions", Some(r#"{"model":"m1","messages":[]}"#)).await;
    assert_eq!(s, StatusCode::GATEWAY_TIMEOUT);
    assert_eq!(kode_error(&b), "upstream_timeout");
}

fn muat(teks: &str) -> anyhow::Result<Config> {
    Config::from_toml_str(teks, &|_| None)
}

#[test]
fn config_valid_dan_base_url_dinormalkan() {
    let c = muat("[[model]]\nalias=\"a\"\n[[model.upstream]]\nbase_url=\"http://h:1/v1/\"\nmodel=\"x\"\n").unwrap();
    assert_eq!(c.models["a"].upstreams[0].url, "http://h:1/v1/chat/completions");
    assert_eq!(c.listen, "127.0.0.1:4000");
}

#[test]
fn config_menolak_yang_salah() {
    assert!(muat("[[model]]\nalias=\"a\"\n").is_err(), "tanpa upstream");
    assert!(muat("[[model]]\nalias=\"a\"\n[[model.upstream]]\nbase_url=\"ftp://h\"\nmodel=\"x\"\n").is_err(), "skema salah");
    assert!(muat("[[model]]\nalias=\"a\"\n[[model.upstream]]\nbase_url=\"http://h\"\nmodel=\"x\"\ntimeout_secs=0\n").is_err(), "timeout 0");
    let ganda = "[[model]]\nalias=\"a\"\n[[model.upstream]]\nbase_url=\"http://h\"\nmodel=\"x\"\n[[model]]\nalias=\"a\"\n[[model.upstream]]\nbase_url=\"http://h\"\nmodel=\"y\"\n";
    assert!(muat(ganda).is_err(), "alias ganda");
    assert!(muat("[server]\nmax_body_mb=0\n").is_err(), "max_body_mb 0");
}
