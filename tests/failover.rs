use std::sync::{
    Arc, Mutex,
    atomic::{AtomicUsize, Ordering},
};
use std::time::{Duration, Instant};

use axum::{
    Json, Router,
    body::Body,
    extract::State,
    http::{HeaderValue, Method, Request, StatusCode},
    response::IntoResponse,
    routing::post,
};
use http_body_util::BodyExt;
use nigate::{AppState, app, config::Config, keys::KeyStore};
use serde_json::{Value, json};
use tower::ServiceExt;

mod common;
use common::jalankan_upstream;

const CHAT: &str = r#"{"model":"m1","messages":[{"role":"user","content":"hi"}]}"#;

// ---------- upstream palsu ----------

struct Balas {
    status: u16,
    retry_after: Option<u64>,
    tunda_ms: u64,
}

fn ok() -> Balas {
    Balas { status: 200, retry_after: None, tunda_ms: 0 }
}
fn status(s: u16) -> Balas {
    Balas { status: s, retry_after: None, tunda_ms: 0 }
}

#[derive(Clone)]
struct Palsu {
    hits: Arc<AtomicUsize>,
    model_diterima: Arc<Mutex<Vec<String>>>,
    perilaku: Arc<dyn Fn(usize) -> Balas + Send + Sync>,
}

impl Palsu {
    fn baru(perilaku: impl Fn(usize) -> Balas + Send + Sync + 'static) -> Self {
        Self { hits: Arc::default(), model_diterima: Arc::default(), perilaku: Arc::new(perilaku) }
    }
    fn hits(&self) -> usize {
        self.hits.load(Ordering::SeqCst)
    }
    async fn jalan(&self) -> String {
        let r = Router::new()
            .route(
                "/v1/chat/completions",
                post(|State(p): State<Palsu>, Json(b): Json<Value>| async move {
                    let n = p.hits.fetch_add(1, Ordering::SeqCst);
                    p.model_diterima.lock().unwrap().push(b["model"].as_str().unwrap_or("").to_string());
                    let r = (p.perilaku)(n);
                    if r.tunda_ms > 0 {
                        tokio::time::sleep(Duration::from_millis(r.tunda_ms)).await;
                    }
                    let mut resp = (
                        StatusCode::from_u16(r.status).unwrap(),
                        Json(json!({"ok": r.status < 400, "n": n, "usage": {"total_tokens": 5}})),
                    )
                        .into_response();
                    if let Some(ra) = r.retry_after {
                        resp.headers_mut().insert("retry-after", HeaderValue::from(ra));
                    }
                    resp
                }),
            )
            .with_state(self.clone());
        jalankan_upstream(r).await
    }
}

// ---------- gateway ----------

const MATI: &str = "http://127.0.0.1:1/v1";

/// `ups` = (nama, base_url, model). Semua ditaruh di alias "m1", ditambah resilience yang cepat untuk tes.
fn buat(ups: &[(&str, &str, &str)], resilience: &str) -> (Router, AppState) {
    let mut t = format!("[auth]\nrequired = false\n[resilience]\nretry_backoff_ms = 5\n{resilience}\n[[model]]\nalias = \"m1\"\n");
    for (nama, base, model) in ups {
        t += &format!("[[model.upstream]]\nname = \"{nama}\"\nbase_url = \"{base}\"\nmodel = \"{model}\"\ntimeout_secs = 5\n");
    }
    let cfg = Config::from_toml_str(&t, &|_| None).unwrap();
    let st = AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap())).unwrap();
    (app(st.clone()), st)
}

struct Jawab {
    status: StatusCode,
    upstream: Option<String>,
    percobaan: Option<u32>,
    body: String,
}

async fn chat(r: &Router) -> Jawab {
    let req = Request::builder()
        .method(Method::POST)
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .body(Body::from(CHAT))
        .unwrap();
    let resp = r.clone().oneshot(req).await.unwrap();
    let h = |n: &str| resp.headers().get(n).map(|v| v.to_str().unwrap().to_string());
    let (upstream, percobaan) = (h("x-nigate-upstream"), h("x-nigate-attempts").map(|v| v.parse().unwrap()));
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    Jawab { status, upstream, percobaan, body: String::from_utf8_lossy(&bytes).to_string() }
}

// ---------- tes ----------

#[tokio::test]
async fn upstream_5xx_dicoba_ulang_lalu_pindah_dan_model_diganti_per_upstream() {
    let a = Palsu::baru(|_| status(500));
    let b = Palsu::baru(|_| ok());
    let (ua, ub) = (a.jalan().await, b.jalan().await);
    let (r, _) = buat(&[("a", &ua, "model-a"), ("b", &ub, "model-b")], "");

    let j = chat(&r).await;
    assert_eq!(j.status, StatusCode::OK);
    assert_eq!(j.upstream.as_deref(), Some("b"));
    assert_eq!(j.percobaan, Some(3), "a: 1 + 1 retry, lalu b");
    assert_eq!(a.hits(), 2);
    assert_eq!(*a.model_diterima.lock().unwrap(), vec!["model-a", "model-a"]);
    assert_eq!(*b.model_diterima.lock().unwrap(), vec!["model-b"]);
}

#[tokio::test]
async fn retry_di_upstream_yang_sama_bisa_menyelamatkan_request() {
    let a = Palsu::baru(|n| if n == 0 { status(503) } else { ok() });
    let ua = a.jalan().await;
    let (r, _) = buat(&[("a", &ua, "x")], "");
    let j = chat(&r).await;
    assert_eq!((j.status, j.percobaan, j.upstream.as_deref()), (StatusCode::OK, Some(2), Some("a")));
}

#[tokio::test]
async fn max_retries_nol_mematikan_retry() {
    let a = Palsu::baru(|_| status(500));
    let b = Palsu::baru(|_| ok());
    let (ua, ub) = (a.jalan().await, b.jalan().await);
    let (r, _) = buat(&[("a", &ua, "x"), ("b", &ub, "x")], "max_retries = 0");
    let j = chat(&r).await;
    assert_eq!((j.status, j.percobaan), (StatusCode::OK, Some(2)));
    assert_eq!(a.hits(), 1);
}

#[tokio::test]
async fn upstream_429_dan_401_langsung_pindah_tanpa_retry() {
    for kode in [429u16, 401, 403] {
        let a = Palsu::baru(move |_| status(kode));
        let b = Palsu::baru(|_| ok());
        let (ua, ub) = (a.jalan().await, b.jalan().await);
        let (r, _) = buat(&[("a", &ua, "x"), ("b", &ub, "x")], "");
        let j = chat(&r).await;
        assert_eq!((j.status, j.percobaan), (StatusCode::OK, Some(2)), "kode {kode}");
        assert_eq!(a.hits(), 1, "kode {kode} tidak boleh di-retry");
    }
}

#[tokio::test]
async fn galat_klien_400_diteruskan_tanpa_failover_dan_tidak_membuat_upstream_dingin() {
    let a = Palsu::baru(|_| status(400));
    let b = Palsu::baru(|_| ok());
    let (ua, ub) = (a.jalan().await, b.jalan().await);
    let (r, st) = buat(&[("a", &ua, "x"), ("b", &ub, "x")], "");
    let j = chat(&r).await;
    assert_eq!((j.status, j.percobaan), (StatusCode::BAD_REQUEST, Some(1)));
    assert_eq!(j.upstream.as_deref(), Some("a"));
    assert_eq!(b.hits(), 0);
    let id_a = format!("{ua}|x");
    assert_eq!(st.kesehatan.status(&id_a, Instant::now()).gagal_beruntun, 0);
}

#[tokio::test]
async fn koneksi_gagal_pindah_ke_upstream_berikutnya() {
    let b = Palsu::baru(|_| ok());
    let ub = b.jalan().await;
    let (r, _) = buat(&[("mati", MATI, "x"), ("b", &ub, "x")], "");
    let j = chat(&r).await;
    assert_eq!((j.status, j.upstream.as_deref(), j.percobaan), (StatusCode::OK, Some("b"), Some(3)));
}

#[tokio::test]
async fn semua_gagal_meneruskan_kegagalan_terakhir() {
    let a = Palsu::baru(|_| status(500));
    let b = Palsu::baru(|_| status(503));
    let (ua, ub) = (a.jalan().await, b.jalan().await);
    let (r, _) = buat(&[("a", &ua, "x"), ("b", &ub, "x")], "");
    let j = chat(&r).await;
    assert_eq!(j.status, StatusCode::SERVICE_UNAVAILABLE, "kegagalan terakhir (b) diteruskan");
    assert_eq!(j.percobaan, Some(4));
    assert!(j.body.contains("\"ok\":false"));

    let (r, _) = buat(&[("mati1", MATI, "x"), ("mati2", MATI, "x")], "");
    let j = chat(&r).await;
    assert_eq!(j.status, StatusCode::BAD_GATEWAY);
    assert!(j.body.contains("upstream_unreachable") && !j.body.contains("127.0.0.1"));
}

#[tokio::test]
async fn upstream_gagal_masuk_cooldown_dan_dilewati_request_berikutnya() {
    let a = Palsu::baru(|_| status(500));
    let b = Palsu::baru(|_| ok());
    let (ua, ub) = (a.jalan().await, b.jalan().await);
    let (r, st) = buat(&[("a", &ua, "x"), ("b", &ub, "x")], "cooldown_secs = 60");

    assert_eq!(chat(&r).await.percobaan, Some(3));
    let s = st.kesehatan.status(&format!("{ua}|x"), Instant::now());
    assert_eq!(s.gagal_beruntun, 1);
    assert!(s.sisa_cooldown.unwrap() > Duration::from_secs(55));

    let j = chat(&r).await;
    assert_eq!((j.upstream.as_deref(), j.percobaan), (Some("b"), Some(1)), "a dilewati, langsung b");
    assert_eq!(a.hits(), 2, "a tidak dipanggil lagi");
}

#[tokio::test]
async fn cooldown_habis_upstream_dicoba_lagi_dan_pulih() {
    let a = Palsu::baru(|n| if n < 2 { status(500) } else { ok() });
    let b = Palsu::baru(|_| ok());
    let (ua, ub) = (a.jalan().await, b.jalan().await);
    let (r, st) = buat(&[("a", &ua, "x"), ("b", &ub, "x")], "cooldown_secs = 1");

    assert_eq!(chat(&r).await.upstream.as_deref(), Some("b"));
    tokio::time::sleep(Duration::from_millis(1200)).await;
    let j = chat(&r).await;
    assert_eq!(j.upstream.as_deref(), Some("a"), "a sudah pulih dan kembali jadi prioritas");
    assert_eq!(st.kesehatan.status(&format!("{ua}|x"), Instant::now()).gagal_beruntun, 0);
}

#[tokio::test]
async fn semua_upstream_dingin_tetap_dicoba_sekali_tanpa_retry() {
    let a = Palsu::baru(|_| status(500));
    let ua = a.jalan().await;
    let (r, _) = buat(&[("a", &ua, "x")], "cooldown_secs = 60");
    assert_eq!(chat(&r).await.percobaan, Some(2));
    let j = chat(&r).await;
    assert_eq!((j.status, j.percobaan), (StatusCode::INTERNAL_SERVER_ERROR, Some(1)), "probe tunggal");
    assert_eq!(a.hits(), 3);
}

#[tokio::test]
async fn cooldown_429_memakai_retry_after_dari_provider() {
    let a = Palsu::baru(|_| Balas { status: 429, retry_after: Some(100), tunda_ms: 0 });
    let b = Palsu::baru(|_| ok());
    let (ua, ub) = (a.jalan().await, b.jalan().await);
    let (r, st) = buat(&[("a", &ua, "x"), ("b", &ub, "x")], "cooldown_secs = 5");
    chat(&r).await;
    let sisa = st.kesehatan.status(&format!("{ua}|x"), Instant::now()).sisa_cooldown.unwrap();
    assert!(sisa > Duration::from_secs(90), "harus ikut Retry-After 100 dtk, bukan cooldown 5 dtk: {sisa:?}");
}

#[tokio::test]
async fn kesehatan_dipakai_bersama_antar_alias_yang_menunjuk_upstream_sama() {
    let a = Palsu::baru(|_| status(500));
    let b = Palsu::baru(|_| ok());
    let (ua, ub) = (a.jalan().await, b.jalan().await);
    let t = format!(
        "[auth]\nrequired = false\n[resilience]\nretry_backoff_ms = 5\ncooldown_secs = 60\n\
         [[model]]\nalias = \"m1\"\n[[model.upstream]]\nname = \"a\"\nbase_url = \"{ua}\"\nmodel = \"x\"\n[[model.upstream]]\nname = \"b\"\nbase_url = \"{ub}\"\nmodel = \"x\"\n\
         [[model]]\nalias = \"m2\"\n[[model.upstream]]\nname = \"a\"\nbase_url = \"{ua}\"\nmodel = \"x\"\n[[model.upstream]]\nname = \"b\"\nbase_url = \"{ub}\"\nmodel = \"x\"\n"
    );
    let cfg = Config::from_toml_str(&t, &|_| None).unwrap();
    let r = app(AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap())).unwrap());

    assert_eq!(chat(&r).await.percobaan, Some(3)); // m1 menemukan a rusak
    let req = Request::builder()
        .method(Method::POST)
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"model":"m2","messages":[]}"#))
        .unwrap();
    let resp = r.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.headers()["x-nigate-attempts"], "1", "m2 langsung ke b karena a sudah dingin");
    assert_eq!(resp.headers()["x-nigate-upstream"], "b");
}

#[tokio::test]
async fn total_timeout_membatasi_seluruh_percobaan() {
    let a = Palsu::baru(|_| Balas { status: 200, retry_after: None, tunda_ms: 3000 });
    let ua = a.jalan().await;
    let (r, _) = buat(&[("a", &ua, "x")], "max_retries = 3\ntotal_timeout_secs = 1");
    let mulai = Instant::now();
    let j = chat(&r).await;
    assert_eq!(j.status, StatusCode::GATEWAY_TIMEOUT);
    assert!(mulai.elapsed() < Duration::from_millis(2500), "harus berhenti sekitar 1 dtk, bukan 3 x 5 dtk: {:?}", mulai.elapsed());
}

#[tokio::test]
async fn upstream_tanpa_key_di_env_dilewati_dan_bukan_kegagalan() {
    let b = Palsu::baru(|_| ok());
    let ub = b.jalan().await;
    let t = format!(
        "[auth]\nrequired = false\n[[model]]\nalias = \"m1\"\n\
         [[model.upstream]]\nname = \"a\"\nbase_url = \"http://127.0.0.1:1/v1\"\nmodel = \"x\"\napi_key_env = \"TIDAK_ADA\"\n\
         [[model.upstream]]\nname = \"b\"\nbase_url = \"{ub}\"\nmodel = \"x\"\n"
    );
    let cfg = Config::from_toml_str(&t, &|_| None).unwrap();
    let r = app(AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap())).unwrap());
    let j = chat(&r).await;
    assert_eq!((j.status, j.percobaan, j.upstream.as_deref()), (StatusCode::OK, Some(1), Some("b")));
}

#[test]
fn config_resilience_default_dan_validasi() {
    let dasar = "[[model]]\nalias=\"a\"\n[[model.upstream]]\nbase_url=\"http://h\"\nmodel=\"x\"\n";
    let c = Config::from_toml_str(dasar, &|_| None).unwrap();
    assert_eq!((c.max_retries, c.cooldown, c.total_timeout), (1, Duration::from_secs(30), Duration::from_secs(300)));
    assert_eq!(c.models["a"].upstreams[0].name, "upstream-1");

    for buruk in ["max_retries = 6", "cooldown_secs = 0", "total_timeout_secs = 0", "total_timeout_secs = 1801", "retry_backoff_ms = 10001"]
    {
        assert!(Config::from_toml_str(&format!("[resilience]\n{buruk}\n{dasar}"), &|_| None).is_err(), "harus ditolak: {buruk}");
    }
}

#[test]
fn config_menolak_nama_upstream_ganda_atau_tidak_valid() {
    let dua = |n1: &str, n2: &str| {
        format!(
            "[[model]]\nalias=\"a\"\n[[model.upstream]]\nname=\"{n1}\"\nbase_url=\"http://h\"\nmodel=\"x\"\n[[model.upstream]]\nname=\"{n2}\"\nbase_url=\"http://h\"\nmodel=\"y\"\n"
        )
    };
    assert!(Config::from_toml_str(&dua("p", "q"), &|_| None).is_ok());
    assert!(Config::from_toml_str(&dua("p", "p"), &|_| None).is_err());
    assert!(Config::from_toml_str(&dua("p", "spasi salah"), &|_| None).is_err());
}
