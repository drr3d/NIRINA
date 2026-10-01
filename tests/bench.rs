//! Benchmark ringan untuk jalur panas. Tidak berjalan di `cargo test` biasa (ditandai #[ignore]); jalankan di mode release:
//!   cargo test --release --test bench -- --ignored --nocapture

use std::sync::Arc;
use std::time::Instant;

use axum::{
    Json, Router,
    body::Body,
    http::{Method, Request, StatusCode},
    routing::post,
};
use http_body_util::BodyExt;
use nigate::{
    AppState, app,
    config::Config,
    guardrail::{Guardrail, GuardrailCfg, Laporan},
    keys::KeyStore,
};
use serde_json::json;
use tower::ServiceExt;

mod common;
use common::jalankan_upstream;

fn mb_per_dtk(byte: usize, dtk: f64) -> f64 {
    byte as f64 / 1_048_576.0 / dtk
}

#[test]
#[ignore]
fn guardrail_throughput() {
    let g = Guardrail::baru(&GuardrailCfg::default()).unwrap();
    let bersih = "Penjualan toko naik 12% dibanding bulan lalu, sementara kategori lain turun 3% bulan ini. ".repeat(12_000); // ~1 MB
    // Token palsu dirakit dari potongan supaya file ini tidak memicu pemindai secret.
    let rahasia = format!(
        "{} kunci {}_{} dan DB_PASSWORD={} ",
        "data biasa ".repeat(30),
        "ghp",
        "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5",
        "example-pw-1234"
    );
    let banyak = rahasia.repeat(3_000);

    for (nama, teks) in [("teks bersih", &bersih), ("banyak rahasia", &banyak)] {
        let mulai = Instant::now();
        let mut lap = Laporan::default();
        let hasil = g.periksa(teks, &mut lap);
        let dt = mulai.elapsed().as_secs_f64();
        println!(
            "guardrail {nama:<15}: {:>7.1} MB/dtk  ({:.0} KB, {} temuan, {:.0} ms)",
            mb_per_dtk(teks.len(), dt),
            teks.len() as f64 / 1024.0,
            lap.total(),
            dt * 1000.0
        );
        assert!(hasil.len() <= teks.len() + 64 * lap.total() as usize);
    }
}

#[test]
#[ignore]
fn autentikasi_key_per_detik() {
    let keys = KeyStore::open_memory().unwrap();
    let (_, token) = keys.create("a").unwrap();
    for i in 0..200 {
        keys.create(&format!("k{i}")).unwrap();
    }
    let n = 500_000;
    let mulai = Instant::now();
    let mut ok = 0u64;
    for _ in 0..n {
        ok += keys.authenticate(&token).is_some() as u64;
    }
    let dt = mulai.elapsed().as_secs_f64();
    assert_eq!(ok, n);
    println!("autentikasi key: {:.0} lookup/dtk ({:.0} ns/lookup)", n as f64 / dt, dt * 1e9 / n as f64);
}

async fn kirim(r: &Router, uri: &str, token: &str, body: &str) -> StatusCode {
    let req = Request::builder()
        .method(Method::POST)
        .uri(uri)
        .header("content-type", "application/json")
        .header("authorization", format!("Bearer {token}"))
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = r.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let _ = resp.into_body().collect().await;
    status
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore]
async fn overhead_gateway_per_request() {
    let upstream = Router::new().route(
        "/v1/chat/completions",
        post(|| async { Json(json!({"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"total_tokens": 10}})) }),
    );
    let base = jalankan_upstream(upstream).await;
    let cfg =
        Config::from_toml_str(&format!("[[model]]\nalias = \"m1\"\n[[model.upstream]]\nbase_url = \"{base}\"\nmodel = \"x\"\n"), &|_| None)
            .unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    let (_, token) = keys.create("a").unwrap();
    keys.set_limits("a", Some(1_000_000), Some(1_000_000_000)).unwrap();
    let gateway = app(AppState::new(cfg, keys).unwrap());

    let body =
        json!({"model": "m1", "messages": [{"role": "user", "content": "Ringkas penjualan bulan ini untuk semua produk."}]}).to_string();
    let langsung =
        json!({"model": "x", "messages": [{"role": "user", "content": "Ringkas penjualan bulan ini untuk semua produk."}]}).to_string();
    let klien = reqwest::Client::new();
    let url_langsung = format!("{base}/chat/completions");

    for _ in 0..200 {
        kirim(&gateway, "/v1/chat/completions", &token, &body).await;
        klien.post(&url_langsung).header("content-type", "application/json").body(langsung.clone()).send().await.unwrap();
    }
    let n = 3000;
    let t = Instant::now();
    for _ in 0..n {
        assert_eq!(kirim(&gateway, "/v1/chat/completions", &token, &body).await, StatusCode::OK);
    }
    let lewat_gateway = t.elapsed().as_secs_f64() / n as f64;
    let t = Instant::now();
    for _ in 0..n {
        klien
            .post(&url_langsung)
            .header("content-type", "application/json")
            .body(langsung.clone())
            .send()
            .await
            .unwrap()
            .bytes()
            .await
            .unwrap();
    }
    let langsung_dtk = t.elapsed().as_secs_f64() / n as f64;
    println!(
        "request berurutan: lewat gateway {:.0} us, langsung ke upstream {:.0} us => overhead gateway {:.0} us/request",
        lewat_gateway * 1e6,
        langsung_dtk * 1e6,
        (lewat_gateway - langsung_dtk) * 1e6
    );

    // konkuren
    let konkuren = 64;
    let per_tugas = 100;
    let t = Instant::now();
    let mut tugas = Vec::new();
    for _ in 0..konkuren {
        let (g, tok, b) = (gateway.clone(), token.clone(), body.clone());
        tugas.push(tokio::spawn(async move {
            for _ in 0..per_tugas {
                assert_eq!(kirim(&g, "/v1/chat/completions", &tok, &b).await, StatusCode::OK);
            }
        }));
    }
    for h in tugas {
        h.await.unwrap();
    }
    let total = (konkuren * per_tugas) as f64;
    println!("konkuren {konkuren}: {:.0} request/dtk lewat gateway", total / t.elapsed().as_secs_f64());
}
