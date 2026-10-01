//! Tes ketahanan: masukan tak tepercaya (usage dari upstream, ukuran respons) tidak boleh membuat gateway panik,
//! salah hitung, atau kehabisan memori.

use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::{
    Json, Router,
    body::Body,
    http::{Method, Request, StatusCode},
    routing::post,
};
use http_body_util::BodyExt;
use nigate::{AppState, app, config::Config, keys::KeyStore, limiter::Limiter};
use serde_json::{Value, json};
use tower::ServiceExt;

mod common;
use common::{jalankan_upstream, kode_error};

// ---------- limiter: utang TPM ekstrem ----------

#[test]
fn utang_tpm_ekstrem_tidak_membuat_panik_dan_waktu_tunggu_dibatasi() {
    let l = Limiter::default();
    let t0 = Instant::now();
    assert!(l.coba(1, None, Some(1), 1, t0).is_ok());
    l.koreksi_token(1, i64::MAX); // pemakaian mustahil besar dari respons yang rusak/jahat
    let hasil = l.coba(1, None, Some(1), 1, t0);
    let tolak = hasil.expect_err("harus ditolak, bukan panik");
    assert!(tolak.tunggu() <= Duration::from_secs(3600), "tunggu harus dibatasi wajar, dapat {:?}", tolak.tunggu());
    assert!(tolak.retry_after_detik() <= 3600);
}

#[test]
fn koreksi_negatif_ekstrem_tidak_menciptakan_kuota_tak_terbatas() {
    let l = Limiter::default();
    let t0 = Instant::now();
    assert!(l.coba(1, None, Some(1000), 1, t0).is_ok());
    l.koreksi_token(1, i64::MIN); // "pengembalian" mustahil besar
    assert!(l.coba(1, None, Some(1000), 900, t0).is_ok());
    assert!(l.coba(1, None, Some(1000), 900, t0).is_err(), "saldo tetap dibatasi kapasitas, tidak menjadi tak terhingga");
}

// ---------- HTTP: usage dari upstream tak tepercaya ----------

fn upstream_usage(usage: Value) -> Router {
    Router::new().route(
        "/v1/chat/completions",
        post(move || {
            let u = usage.clone();
            async move { Json(json!({"choices": [], "usage": u})) }
        }),
    )
}

async fn chat(r: &Router, token: &str) -> (StatusCode, String, Option<String>) {
    let req = Request::builder()
        .method(Method::POST)
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .header("authorization", format!("Bearer {token}"))
        .body(Body::from(r#"{"model":"m1","messages":[]}"#))
        .unwrap();
    let resp = r.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let ra = resp.headers().get("retry-after").map(|v| v.to_str().unwrap().to_string());
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).to_string(), ra)
}

fn sistem(base: &str, ekstra: &str, tpm: u64) -> (Router, String) {
    let teks = format!("{ekstra}\n[[model]]\nalias = \"m1\"\n[[model.upstream]]\nbase_url = \"{base}\"\nmodel = \"asli\"\n");
    let cfg = Config::from_toml_str(&teks, &|_| None).unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    let (_, token) = keys.create("a").unwrap();
    keys.set_limits("a", None, Some(tpm)).unwrap();
    (app(AppState::new(cfg, keys).unwrap()), token)
}

#[tokio::test]
async fn usage_u64_maksimum_tidak_membalik_tanda_dan_tidak_panik() {
    for usage in [
        json!({"total_tokens": u64::MAX}),
        json!({"total_tokens": i64::MAX as u64 + 1}),
        json!({"prompt_tokens": u64::MAX, "completion_tokens": u64::MAX}),
    ] {
        let base = jalankan_upstream(upstream_usage(usage.clone())).await;
        let (r, token) = sistem(&base, "", 1);
        assert_eq!(chat(&r, &token).await.0, StatusCode::OK, "{usage}");
        let (status, body, ra) = chat(&r, &token).await;
        assert_eq!(
            status,
            StatusCode::TOO_MANY_REQUESTS,
            "pemakaian raksasa harus menahan request berikutnya, bukan mengembalikan kuota: {usage} -> {body}"
        );
        assert!(ra.unwrap().parse::<u64>().unwrap() <= 3600, "Retry-After dibatasi");
        assert_eq!(kode_error(&body), "rate_limit_exceeded");
    }
}

// ---------- ukuran respons upstream ----------

fn upstream_besar(byte: usize) -> Router {
    Router::new().route("/v1/chat/completions", post(move || async move { "x".repeat(byte) }))
}

async fn chat_biasa(r: &Router, token: &str) -> (StatusCode, String) {
    let (s, b, _) = chat(r, token).await;
    (s, b)
}

#[tokio::test]
async fn respons_upstream_melebihi_batas_ditolak_lalu_pindah_ke_upstream_berikutnya() {
    let besar = jalankan_upstream(upstream_besar(2 * 1024 * 1024)).await;
    let sehat = jalankan_upstream(upstream_usage(json!({"total_tokens": 3}))).await;
    let teks = format!(
        "[server]\nmax_response_mb = 1\n[[model]]\nalias = \"m1\"\n[[model.upstream]]\nname = \"besar\"\nbase_url = \"{besar}\"\nmodel = \"x\"\n[[model.upstream]]\nname = \"sehat\"\nbase_url = \"{sehat}\"\nmodel = \"x\"\n"
    );
    let cfg = Config::from_toml_str(&teks, &|_| None).unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    let (_, token) = keys.create("a").unwrap();
    let r = app(AppState::new(cfg, keys).unwrap());
    let (status, _, _) = chat(&r, &token).await;
    assert_eq!(status, StatusCode::OK, "upstream pertama terlalu besar -> failover ke yang sehat");
}

#[tokio::test]
async fn respons_terlalu_besar_tanpa_cadangan_dijawab_502_bukan_menghabiskan_memori() {
    let besar = jalankan_upstream(upstream_besar(3 * 1024 * 1024)).await;
    let teks =
        format!("[server]\nmax_response_mb = 1\n[[model]]\nalias = \"m1\"\n[[model.upstream]]\nbase_url = \"{besar}\"\nmodel = \"x\"\n");
    let cfg = Config::from_toml_str(&teks, &|_| None).unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    let (_, token) = keys.create("a").unwrap();
    let r = app(AppState::new(cfg, keys).unwrap());
    let (status, body) = chat_biasa(&r, &token).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert_eq!(kode_error(&body), "upstream_response_too_large");
}

#[tokio::test]
async fn respons_tepat_di_bawah_batas_tetap_lolos() {
    let base = jalankan_upstream(upstream_besar(900 * 1024)).await;
    let teks =
        format!("[server]\nmax_response_mb = 1\n[[model]]\nalias = \"m1\"\n[[model.upstream]]\nbase_url = \"{base}\"\nmodel = \"x\"\n");
    let cfg = Config::from_toml_str(&teks, &|_| None).unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    let (_, token) = keys.create("a").unwrap();
    let r = app(AppState::new(cfg, keys).unwrap());
    assert_eq!(chat_biasa(&r, &token).await.0, StatusCode::OK);
}

// ---------- payload besar (jalur thread pemblokir) ----------

#[tokio::test]
async fn payload_besar_tetap_diproses_dan_diredaksi_lewat_thread_pemblokir() {
    use common::{Rekam, upstream_normal};
    let rekam: Rekam = Arc::default();
    let base = jalankan_upstream(upstream_normal(rekam.clone())).await;
    let cfg = Config::from_toml_str(
        &format!("[[model]]\nalias = \"m1\"\n[[model.upstream]]\nbase_url = \"{base}\"\nmodel = \"asli\"\n"),
        &|_| None,
    )
    .unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    let (_, token) = keys.create("a").unwrap();
    let r = app(AppState::new(cfg, keys).unwrap());

    let rahasia = format!("{}_{}", "ghp", "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5");
    let isi = format!("{} kunci {rahasia} {}", "penjualan naik. ".repeat(20_000), "selesai.".repeat(10)); // ~320 KB > ambang 64 KB
    let body = json!({"model": "m1", "messages": [{"role": "user", "content": isi}]}).to_string();
    assert!(body.len() > 64 * 1024);
    let req = Request::builder()
        .method(Method::POST)
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .header("authorization", format!("Bearer {token}"))
        .body(Body::from(body))
        .unwrap();
    assert_eq!(r.clone().oneshot(req).await.unwrap().status(), StatusCode::OK);
    let diterima = rekam.lock().unwrap();
    let sampai = diterima[0].1["messages"][0]["content"].as_str().unwrap();
    assert!(!sampai.contains(&rahasia) && sampai.contains("[REDACTED:github_token]"));
}

// ---------- util ----------

#[test]
fn kunci_pulih_dari_mutex_yang_teracuni() {
    use nigate::util::kunci;
    use std::sync::Mutex;
    let m = Arc::new(Mutex::new(41));
    let m2 = Arc::clone(&m);
    let _ = std::thread::spawn(move || {
        let _g = m2.lock().unwrap();
        panic!("sengaja: meracuni mutex");
    })
    .join();
    assert!(m.is_poisoned());
    *kunci(&m) += 1;
    assert_eq!(*kunci(&m), 42, "tetap bisa dipakai setelah thread lain panik");
}

#[test]
fn ke_i64_menjepit_bukan_membungkus() {
    use nigate::util::ke_i64;
    assert_eq!(ke_i64(5), 5);
    assert_eq!(ke_i64(u64::MAX), i64::MAX);
    assert_eq!(ke_i64(i64::MAX as u64 + 1), i64::MAX);
}

#[test]
fn pembatas_log_membatasi_satu_pesan_per_selang() {
    use nigate::util::PembatasLog;
    let p = PembatasLog::baru(10_000);
    assert!(p.boleh_pada(50_000));
    assert!(!p.boleh_pada(50_001));
    assert!(!p.boleh_pada(59_999));
    assert!(p.boleh_pada(60_000));
}

#[test]
fn config_batas_respons_dan_tenggang_shutdown_divalidasi() {
    let c = Config::from_toml_str("", &|_| None).unwrap();
    assert_eq!((c.max_response_bytes, c.shutdown_grace), (32 * 1024 * 1024, Duration::from_secs(30)));
    for buruk in ["max_response_mb = 0", "max_response_mb = 257", "shutdown_grace_secs = 0", "shutdown_grace_secs = 301"] {
        assert!(Config::from_toml_str(&format!("[server]\n{buruk}\n"), &|_| None).is_err(), "harus ditolak: {buruk}");
    }
}
