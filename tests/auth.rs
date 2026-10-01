use std::sync::Arc;

use axum::{
    Router,
    http::{Method, Request, StatusCode},
};
use nigate::{AppState, app, config::Config, keys::KeyStore};
use tower::ServiceExt;

mod common;
use common::*;

fn buat(base: &str, wajib: bool) -> (Router, Arc<KeyStore>) {
    let teks =
        format!("[auth]\nrequired = {wajib}\n[[model]]\nalias = \"m1\"\n[[model.upstream]]\nbase_url = \"{base}\"\nmodel = \"asli\"\n");
    let cfg = Config::from_toml_str(&teks, &|_| None).unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    (app(AppState::new(cfg, keys.clone()).unwrap()), keys)
}

const CHAT: &str = r#"{"model":"m1","messages":[{"role":"user","content":"hi"}]}"#;

async fn chat_dengan(router: Router, header_auth: Option<&str>) -> (StatusCode, String) {
    let mut rb = Request::builder().method(Method::POST).uri("/v1/chat/completions").header("content-type", "application/json");
    if let Some(h) = header_auth {
        rb = rb.header("authorization", h);
    }
    let resp = router.oneshot(rb.body(axum::body::Body::from(CHAT)).unwrap()).await.unwrap();
    let status = resp.status();
    let bytes = http_body_util::BodyExt::collect(resp.into_body()).await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).to_string())
}

#[tokio::test]
async fn tanpa_header_401() {
    let (r, _) = buat("http://127.0.0.1:1/v1", true);
    let (s, b) = chat_dengan(r, None).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED);
    assert_eq!(kode_error(&b), "missing_api_key");
}

#[tokio::test]
async fn key_salah_401_dan_skema_selain_bearer_ditolak() {
    let (r, keys) = buat("http://127.0.0.1:1/v1", true);
    let (_, token) = keys.create("tim-a").unwrap();
    let (s, b) = chat_dengan(r.clone(), Some("Bearer ngk_salah")).await;
    assert_eq!((s, kode_error(&b).as_str()), (StatusCode::UNAUTHORIZED, "invalid_api_key"));
    let (s, b) = chat_dengan(r, Some(&format!("Basic {token}"))).await;
    assert_eq!((s, kode_error(&b).as_str()), (StatusCode::UNAUTHORIZED, "missing_api_key"));
}

#[tokio::test]
async fn key_valid_lolos_dan_token_klien_tidak_bocor_ke_upstream() {
    let rekam: Rekam = Arc::default();
    let base = jalankan_upstream(upstream_normal(rekam.clone())).await;
    let (r, keys) = buat(&base, true);
    let (_, token) = keys.create("tim-a").unwrap();

    let (s, _) = chat_dengan(r.clone(), Some(&format!("Bearer {token}"))).await;
    assert_eq!(s, StatusCode::OK);
    let (s, _) = chat_dengan(r, Some(&format!("bEaReR {token}"))).await;
    assert_eq!(s, StatusCode::OK, "nama skema tidak peka huruf besar/kecil");

    let diterima = rekam.lock().unwrap();
    assert_eq!(diterima.len(), 2);
    assert!(diterima.iter().all(|(auth, _)| auth.is_none()), "key gateway tidak boleh diteruskan ke provider");
}

#[tokio::test]
async fn key_dicabut_ditolak_dan_bisa_diaktifkan_lagi() {
    let base = jalankan_upstream(upstream_normal(Arc::default())).await;
    let (r, keys) = buat(&base, true);
    let (_, token) = keys.create("tim-a").unwrap();
    let h = format!("Bearer {token}");

    assert_eq!(chat_dengan(r.clone(), Some(&h)).await.0, StatusCode::OK);
    assert!(keys.set_active("tim-a", false).unwrap());
    assert_eq!(chat_dengan(r.clone(), Some(&h)).await.0, StatusCode::UNAUTHORIZED);
    assert!(keys.set_active("tim-a", true).unwrap());
    assert_eq!(chat_dengan(r, Some(&h)).await.0, StatusCode::OK);
}

#[tokio::test]
async fn healthz_terbuka_tetapi_models_butuh_key() {
    let (r, keys) = buat("http://127.0.0.1:1/v1", true);
    let (s, _) = kirim(r.clone(), Method::GET, "/healthz", None).await;
    assert_eq!(s, StatusCode::OK);
    let (s, _) = kirim(r.clone(), Method::GET, "/v1/models", None).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED);

    let (_, token) = keys.create("tim-a").unwrap();
    let req =
        Request::builder().uri("/v1/models").header("authorization", format!("Bearer {token}")).body(axum::body::Body::empty()).unwrap();
    assert_eq!(r.oneshot(req).await.unwrap().status(), StatusCode::OK);
}

#[tokio::test]
async fn auth_dimatikan_mengizinkan_tanpa_key() {
    let base = jalankan_upstream(upstream_normal(Arc::default())).await;
    let (r, _) = buat(&base, false);
    assert_eq!(chat_dengan(r, None).await.0, StatusCode::OK);
}
