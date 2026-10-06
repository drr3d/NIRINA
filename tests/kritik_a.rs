//! Regresi dari kritik independen (Batch A): kebijakan request/respons dan urutan pemrosesan. Setiap tes ditulis SEBELUM
//! perbaikannya dan harus gagal pada kode lama.

use std::sync::{
    Arc, Mutex,
    atomic::{AtomicUsize, Ordering},
};

use axum::{
    Router,
    body::Body,
    extract::State,
    http::{HeaderValue, Method, Request, StatusCode, header},
    response::IntoResponse,
    routing::post,
};
use http_body_util::BodyExt;
use nigate::{AppState, app, config::Config, keys::KeyStore};
use serde_json::{Value, json};
use tower::ServiceExt;

mod common;
use common::{jalankan_upstream, kode_error};

/// Upstream palsu yang menjawab dengan (status, content-type, body, header tambahan) tetap dan merekam isi request.
#[derive(Clone)]
struct Tetap {
    status: u16,
    ctype: &'static str,
    isi: String,
    header_tambahan: Vec<(&'static str, String)>,
    hits: Arc<AtomicUsize>,
    diterima: Arc<Mutex<Vec<Value>>>,
}

impl Tetap {
    fn baru(status: u16, ctype: &'static str, isi: &str) -> Self {
        Self { status, ctype, isi: isi.to_string(), header_tambahan: vec![], hits: Arc::default(), diterima: Arc::default() }
    }
    fn json_ok() -> Self {
        Self::baru(200, "application/json", r#"{"choices":[{"message":{"role":"assistant","content":"ok"}}],"usage":{"total_tokens":3}}"#)
    }
    fn hits(&self) -> usize {
        self.hits.load(Ordering::SeqCst)
    }
    async fn jalan(&self) -> String {
        let r = Router::new()
            .route(
                "/v1/chat/completions",
                post(|State(t): State<Tetap>, body: axum::body::Bytes| async move {
                    t.hits.fetch_add(1, Ordering::SeqCst);
                    if let Ok(v) = serde_json::from_slice::<Value>(&body) {
                        t.diterima.lock().unwrap().push(v);
                    }
                    let mut resp = (StatusCode::from_u16(t.status).unwrap(), t.isi.clone()).into_response();
                    resp.headers_mut().insert(header::CONTENT_TYPE, HeaderValue::from_static(t.ctype));
                    for (k, v) in &t.header_tambahan {
                        resp.headers_mut().insert(*k, HeaderValue::from_str(v).unwrap());
                    }
                    resp
                }),
            )
            // Batas body bawaan axum (2 MB) tidak boleh ikut menentukan hasil tes ukuran milik gateway.
            .layer(axum::extract::DefaultBodyLimit::disable())
            .with_state(self.clone());
        jalankan_upstream(r).await
    }
}

fn buat(ekstra: &str, ups: &[(&str, &str)]) -> Router {
    let mut t = format!("[auth]\nrequired = false\n[resilience]\nretry_backoff_ms = 5\n{ekstra}\n[[model]]\nalias = \"m1\"\n");
    for (nama, base) in ups {
        t += &format!("[[model.upstream]]\nname = \"{nama}\"\nbase_url = \"{base}\"\nmodel = \"asli\"\ntimeout_secs = 5\n");
    }
    let cfg = Config::from_toml_str(&t, &|_| None).unwrap();
    app(AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap())).unwrap())
}

const CHAT: &str = r#"{"model":"m1","messages":[{"role":"user","content":"hi"}]}"#;

async fn chat(r: &Router, body: &str) -> (StatusCode, String, Option<String>) {
    let req = Request::builder()
        .method(Method::POST)
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = r.clone().oneshot(req).await.unwrap();
    let (status, up) = (resp.status(), resp.headers().get("x-nigate-upstream").map(|v| v.to_str().unwrap().to_string()));
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).to_string(), up)
}

// ---------- A-4: stream harus benar-benar false ----------

#[tokio::test]
async fn stream_selain_false_atau_null_ditolak() {
    let ok = Tetap::json_ok();
    let r = buat("", &[("a", &ok.jalan().await)]);
    for stream in [r#""true""#, "1", r#""false""#, "true", r#""""#, "[]", "{}"] {
        let body = format!(r#"{{"model":"m1","stream":{stream},"messages":[]}}"#);
        let (s, b, _) = chat(&r, &body).await;
        assert_eq!((s, kode_error(&b).as_str()), (StatusCode::BAD_REQUEST, "stream_unsupported"), "stream={stream}");
    }
    assert_eq!(ok.hits(), 0, "tidak satu pun boleh sampai ke upstream");
    for stream in ["false", "null"] {
        let body = format!(r#"{{"model":"m1","stream":{stream},"messages":[]}}"#);
        assert_eq!(chat(&r, &body).await.0, StatusCode::OK, "stream={stream} harus diterima");
    }
}

#[tokio::test]
async fn upstream_tidak_pernah_menerima_stream_true() {
    let ok = Tetap::json_ok();
    let r = buat("", &[("a", &ok.jalan().await)]);
    chat(&r, r#"{"model":"m1","stream":null,"messages":[]}"#).await;
    chat(&r, r#"{"model":"m1","stream":false,"messages":[]}"#).await;
    for v in ok.diterima.lock().unwrap().iter() {
        assert_ne!(v.get("stream"), Some(&json!(true)));
        assert!(matches!(v.get("stream"), None | Some(Value::Bool(false))), "stream diteruskan hanya false/tidak ada: {v}");
    }
}

// ---------- A-4: 2xx yang bukan JSON adalah upstream rusak ----------

#[tokio::test]
async fn respons_2xx_bukan_json_dianggap_gagal_dan_pindah_upstream() {
    let portal = Tetap::baru(200, "text/html", "<html>captive portal</html>");
    let sehat = Tetap::json_ok();
    let r = buat("", &[("portal", &portal.jalan().await), ("sehat", &sehat.jalan().await)]);
    let (s, _, up) = chat(&r, CHAT).await;
    assert_eq!((s, up.as_deref()), (StatusCode::OK, Some("sehat")), "failover ke upstream yang menjawab JSON");
    assert_eq!(portal.hits(), 1, "tidak di-retry: bukan galat sementara");
}

#[tokio::test]
async fn respons_2xx_bukan_json_tanpa_cadangan_dijawab_502_bukan_diteruskan() {
    let portal = Tetap::baru(200, "text/html", "<html>login dulu</html>");
    let r = buat("", &[("portal", &portal.jalan().await)]);
    let (s, b, _) = chat(&r, CHAT).await;
    assert_eq!((s, kode_error(&b).as_str()), (StatusCode::BAD_GATEWAY, "upstream_invalid_response"));
    assert!(!b.contains("login dulu"), "isi dari upstream rusak tidak boleh diteruskan");
}

#[tokio::test]
async fn respons_2xx_dimulai_kurung_tapi_json_rusak_dijawab_502() {
    let rusak = Tetap::baru(200, "application/json", r#"{"choices": [ {"message": "terpotong"#);
    let r = buat("", &[("rusak", &rusak.jalan().await)]);
    let (s, b, _) = chat(&r, CHAT).await;
    assert_eq!((s, kode_error(&b).as_str()), (StatusCode::BAD_GATEWAY, "upstream_invalid_response"));
}

// ---------- A-5: redirect tidak diikuti ----------

#[tokio::test]
async fn redirect_dari_upstream_tidak_diikuti() {
    let target = Tetap::json_ok();
    let url_target = target.jalan().await;
    let mut pengalih = Tetap::baru(307, "text/plain", "pindah");
    pengalih.header_tambahan = vec![("location", format!("{url_target}/chat/completions"))];
    let r = buat("", &[("pengalih", &pengalih.jalan().await)]);
    let (s, b, _) = chat(&r, CHAT).await;
    assert_eq!(target.hits(), 0, "body request tidak boleh diulang ke alamat yang ditunjuk upstream");
    assert_eq!((s, kode_error(&b).as_str()), (StatusCode::BAD_GATEWAY, "upstream_redirect"));
}

#[tokio::test]
async fn upstream_yang_mengalihkan_dilewati_dan_cadangan_dipakai() {
    let mut pengalih = Tetap::baru(302, "text/plain", "pindah");
    pengalih.header_tambahan = vec![("location", "http://127.0.0.1:1/x".to_string())];
    let sehat = Tetap::json_ok();
    let r = buat("", &[("pengalih", &pengalih.jalan().await), ("sehat", &sehat.jalan().await)]);
    let (s, _, up) = chat(&r, CHAT).await;
    assert_eq!((s, up.as_deref()), (StatusCode::OK, Some("sehat")));
    assert_eq!(pengalih.hits(), 1, "tanpa retry");
}

// ---------- R-5: body 401/403 provider tidak diteruskan ----------

#[tokio::test]
async fn kegagalan_auth_upstream_dijawab_502_generik_tanpa_isi_provider() {
    for kode in [401u16, 403] {
        let up = Tetap::baru(kode, "application/json", r#"{"error":{"message":"Incorrect API key provided: sk-or-v1-abcd********wxyz"}}"#);
        let r = buat("", &[("a", &up.jalan().await)]);
        let (s, b, _) = chat(&r, CHAT).await;
        assert_eq!((s, kode_error(&b).as_str()), (StatusCode::BAD_GATEWAY, "upstream_auth_failed"), "status upstream {kode}");
        assert!(!b.contains("sk-or-v1") && !b.contains("Incorrect"), "isi galat provider bocor ke klien: {b}");
    }
}

#[tokio::test]
async fn status_provider_lain_tetap_diteruskan_apa_adanya() {
    let up = Tetap::baru(429, "application/json", r#"{"error":{"message":"limit provider"}}"#);
    let r = buat("", &[("a", &up.jalan().await)]);
    let (s, b, _) = chat(&r, CHAT).await;
    assert_eq!(s, StatusCode::TOO_MANY_REQUESTS);
    assert!(b.contains("limit provider"));
}

// ---------- R-6: kunci pengalih rute tidak diteruskan ----------

#[tokio::test]
async fn kunci_pengalih_rute_dibuang_dan_parameter_lain_dipertahankan() {
    let up = Tetap::json_ok();
    let r = buat("", &[("a", &up.jalan().await)]);
    let body = json!({
        "model": "m1", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2, "seed": 7, "n": 1,
        "provider": {"order": ["x"]}, "models": ["lain/model"], "route": "fallback", "transforms": ["middle-out"], "plugins": [{"id": "web"}]
    });
    assert_eq!(chat(&r, &body.to_string()).await.0, StatusCode::OK);
    let diterima = up.diterima.lock().unwrap();
    let v = &diterima[0];
    for kunci in ["provider", "models", "route", "transforms", "plugins"] {
        assert!(v.get(kunci).is_none(), "'{kunci}' tidak boleh sampai ke upstream: {v}");
    }
    assert_eq!((v["temperature"].as_f64(), v["seed"].as_i64(), v["model"].as_str()), (Some(0.2), Some(7), Some("asli")));
}

// ---------- A-3: limiter dicek sebelum parse ----------

#[tokio::test]
async fn request_yang_melewati_batas_ditolak_sebelum_body_diparse() {
    let ok = Tetap::json_ok();
    let t = format!("[[model]]\nalias = \"m1\"\n[[model.upstream]]\nbase_url = \"{}\"\nmodel = \"asli\"\n", ok.jalan().await);
    let cfg = Config::from_toml_str(&t, &|_| None).unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    let (_, token) = keys.create("a").unwrap();
    keys.set_limits("a", Some(1), None).unwrap();
    let r = app(AppState::new(cfg, keys).unwrap());

    let kirim_dengan = |body: &'static str| {
        let r = r.clone();
        let token = token.clone();
        async move {
            let req = Request::builder()
                .method(Method::POST)
                .uri("/v1/chat/completions")
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(body))
                .unwrap();
            let resp = r.oneshot(req).await.unwrap();
            let status = resp.status();
            let b = resp.into_body().collect().await.unwrap().to_bytes();
            (status, String::from_utf8_lossy(&b).to_string())
        }
    };
    assert_eq!(kirim_dengan(CHAT_STATIS).await.0, StatusCode::OK);
    // RPM habis: body sampah harus dijawab 429, bukan 400. Bukti bahwa limiter dicek lebih dulu dan tidak ada CPU terbuang untuk parse.
    let (s, b) = kirim_dengan("bukan json sama sekali").await;
    assert_eq!((s, kode_error(&b).as_str()), (StatusCode::TOO_MANY_REQUESTS, "rate_limit_exceeded"));
}

const CHAT_STATIS: &str = r#"{"model":"m1","messages":[{"role":"user","content":"hi"}]}"#;

#[tokio::test]
async fn request_tidak_valid_di_bawah_batas_tetap_tidak_memakai_jatah() {
    let ok = Tetap::json_ok();
    let t = format!("[[model]]\nalias = \"m1\"\n[[model.upstream]]\nbase_url = \"{}\"\nmodel = \"asli\"\n", ok.jalan().await);
    let cfg = Config::from_toml_str(&t, &|_| None).unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    let (_, token) = keys.create("a").unwrap();
    keys.set_limits("a", Some(1), None).unwrap();
    let r = app(AppState::new(cfg, keys).unwrap());
    let kirim_dengan = |body: &'static str| {
        let (r, token) = (r.clone(), token.clone());
        async move {
            let req = Request::builder()
                .method(Method::POST)
                .uri("/v1/chat/completions")
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(body))
                .unwrap();
            r.oneshot(req).await.unwrap().status()
        }
    };
    assert_eq!(kirim_dengan("bukan json").await, StatusCode::BAD_REQUEST);
    assert_eq!(kirim_dengan(r#"{"model":"tidak-ada"}"#).await, StatusCode::NOT_FOUND);
    assert_eq!(kirim_dengan(CHAT_STATIS).await, StatusCode::OK, "jatah RPM masih utuh");
}

// ---------- R-1: batas ukuran bawaan lebih ketat ----------

#[test]
fn batas_ukuran_bawaan_sesuai_pemakaian_chat_nyata() {
    let c = Config::from_toml_str("", &|_| None).unwrap();
    assert_eq!(c.max_body_bytes, 4 * 1024 * 1024, "body request chat berukuran KB; 10 MB terlalu longgar untuk container 512 MB");
    assert_eq!(c.max_response_bytes, 8 * 1024 * 1024);
}

#[tokio::test]
async fn body_di_atas_batas_ukuran_ditolak_413_dan_tidak_sampai_ke_upstream() {
    let ok = Tetap::json_ok();
    let r = buat("", &[("a", &ok.jalan().await)]); // batas bawaan 4 MB
    let besar = json!({"model": "m1", "messages": [{"role": "user", "content": "x".repeat(4_500_000)}]}).to_string();
    let (s, _, _) = chat(&r, &besar).await;
    assert_eq!(s, StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(ok.hits(), 0);
    let hampir = json!({"model": "m1", "messages": [{"role": "user", "content": "x".repeat(3_500_000)}]}).to_string();
    assert_eq!(chat(&r, &hampir).await.0, StatusCode::OK, "di bawah batas tetap diterima");
}
