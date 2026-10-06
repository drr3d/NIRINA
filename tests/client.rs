//! Batas dan statistik per client di bawah satu key: client diidentifikasi dari field standar OpenAI `user` di body request.
//! Dipakai aplikasi yang melayani banyak client lewat satu key gateway (mis. API NIRINA).

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::{
    Json, Router,
    body::Body,
    http::{HeaderMap, Method, Request, StatusCode},
    routing::post,
};
use http_body_util::BodyExt;
use nigate::{
    AppState,
    admin::admin_app,
    app,
    config::Config,
    keys::KeyStore,
    limiter::{Batas, Limiter, Subjek},
    stats::Statistik,
};
use serde_json::{Value, json};
use tower::ServiceExt;

mod common;
use common::{Rekam, jalankan_upstream, upstream_normal};

const ADMIN: &str = "token-admin-uji-yang-cukup-panjang-123456";

struct Tmp(PathBuf);

impl Tmp {
    fn baru(nama: &str) -> Self {
        let p = std::env::temp_dir().join(format!("nigate-client-{nama}-{}", std::process::id()));
        Self::hapus(&p);
        Self(p)
    }
    fn path(&self) -> &str {
        self.0.to_str().unwrap()
    }
    fn hapus(p: &std::path::Path) {
        for a in ["", "-wal", "-shm", "-journal"] {
            let _ = std::fs::remove_file(format!("{}{a}", p.display()));
        }
    }
}

impl Drop for Tmp {
    fn drop(&mut self) {
        Self::hapus(&self.0);
    }
}

struct Sistem {
    data: Router,
    admin: Router,
    stat: Arc<Statistik>,
}

fn sistem(base: &str, stats: &Tmp) -> Sistem {
    let toml = format!(
        "[resilience]\nretry_backoff_ms = 5\nmax_retries = 0\n\
         [[model]]\nalias = \"m1\"\n[[model.upstream]]\nname = \"utama\"\nbase_url = \"{base}\"\nmodel = \"asli\"\napi_key_env = \"K\"\n"
    );
    let env = |n: &str| match n {
        "NIGATE_ADMIN_TOKEN" => Some(ADMIN.to_string()),
        "K" => Some("kunci-provider".to_string()),
        _ => None,
    };
    let cfg = Config::from_toml_str(&toml, &env).unwrap();
    let stat = Arc::new(Statistik::buka(stats.path(), 30).unwrap());
    let state = AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap())).unwrap().dengan_statistik(stat.clone());
    Sistem { data: app(state.clone()), admin: admin_app(state), stat }
}

async fn panggil(r: &Router, metode: Method, uri: &str, token: Option<&str>, body: Option<String>) -> (StatusCode, Value, HeaderMap) {
    let mut rb = Request::builder().method(metode).uri(uri).header("content-type", "application/json");
    if let Some(t) = token {
        rb = rb.header("authorization", format!("Bearer {t}"));
    }
    let resp = r.clone().oneshot(rb.body(Body::from(body.unwrap_or_default())).unwrap()).await.unwrap();
    let (status, headers) = (resp.status(), resp.headers().clone());
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null), headers)
}

async fn adm(s: &Sistem, metode: Method, uri: &str, body: Value) -> (StatusCode, Value) {
    let body = (!body.is_null()).then(|| body.to_string());
    let (st, v, _) = panggil(&s.admin, metode, uri, Some(ADMIN), body).await;
    (st, v)
}

async fn key_baru(s: &Sistem, body: Value) -> String {
    let (st, v) = adm(s, Method::POST, "/admin/keys", body).await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    v["key"].as_str().unwrap().to_string()
}

async fn chat(s: &Sistem, key: &str, user: Value) -> (StatusCode, Value, HeaderMap) {
    let mut body = json!({"model": "m1", "messages": [{"role": "user", "content": "halo"}]});
    if !user.is_null() {
        body["user"] = user;
    }
    panggil(&s.data, Method::POST, "/v1/chat/completions", Some(key), Some(body.to_string())).await
}

async fn info(s: &Sistem, key: &str, query: &str) -> (StatusCode, Value) {
    let (st, v, _) = panggil(&s.data, Method::GET, &format!("/v1/key/info{query}"), Some(key), None).await;
    (st, v)
}

fn kode(v: &Value) -> &str {
    v["error"]["code"].as_str().unwrap_or("")
}

// ---------- jalur chat ----------

#[tokio::test]
async fn label_user_tidak_diteruskan_ke_provider() {
    let t = Tmp::baru("strip");
    let rekam = Rekam::default();
    let base = jalankan_upstream(upstream_normal(rekam.clone())).await;
    let s = sistem(&base, &t);
    let key = key_baru(&s, json!({"name": "api"})).await;
    assert_eq!(chat(&s, &key, json!("mall-a")).await.0, StatusCode::OK);
    let body = &rekam.lock().unwrap()[0].1;
    assert!(body.get("user").is_none(), "{body}");
}

#[tokio::test]
async fn batas_bawaan_per_client_berlaku_terpisah_dan_batas_key_tetap_menutup_total() {
    let t = Tmp::baru("bawaan");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&base, &t);
    let key = key_baru(&s, json!({"name": "api", "rpm": 3, "user_rpm": 1})).await;

    assert_eq!(chat(&s, &key, json!("mall-a")).await.0, StatusCode::OK);
    let (st, v, h) = chat(&s, &key, json!("mall-a")).await;
    assert_eq!((st, kode(&v), v["error"]["param"].as_str()), (StatusCode::TOO_MANY_REQUESTS, "rate_limit_exceeded", Some("user")));
    assert!(v["error"]["message"].as_str().unwrap().contains("mall-a"), "{v}");
    assert!(h.contains_key("retry-after"));

    // Client lain punya ember sendiri.
    assert_eq!(chat(&s, &key, json!("mall-b")).await.0, StatusCode::OK);
    assert_eq!(chat(&s, &key, json!("mall-c")).await.0, StatusCode::OK);
    // Batas key (3/menit) menutup total semua client: penolakan ini milik key, bukan client.
    let (st, v, _) = chat(&s, &key, json!("mall-d")).await;
    assert_eq!((st, v["error"]["param"].clone()), (StatusCode::TOO_MANY_REQUESTS, Value::Null), "{v}");
}

#[tokio::test]
async fn penolakan_client_tidak_memakai_jatah_key() {
    let t = Tmp::baru("tanpa-jatah");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&base, &t);
    let key = key_baru(&s, json!({"name": "api", "rpm": 2, "user_rpm": 1})).await;
    assert_eq!(chat(&s, &key, json!("a")).await.0, StatusCode::OK);
    for _ in 0..3 {
        assert_eq!(chat(&s, &key, json!("a")).await.0, StatusCode::TOO_MANY_REQUESTS);
    }
    // Key masih punya 1 jatah tersisa, jadi client lain lolos.
    assert_eq!(chat(&s, &key, json!("b")).await.0, StatusCode::OK);
}

#[tokio::test]
async fn pengaturan_khusus_client_mengalahkan_bawaan_dan_blokir_403() {
    let t = Tmp::baru("khusus");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&base, &t);
    let key = key_baru(&s, json!({"name": "api", "user_rpm": 1})).await;

    let (st, v) = adm(&s, Method::PUT, "/admin/keys/api/users/vip", json!({"rpm": 3})).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!((v["user"]["rpm_efektif"].as_u64(), v["user"]["active"].as_bool()), (Some(3), Some(true)));
    for _ in 0..3 {
        assert_eq!(chat(&s, &key, json!("vip")).await.0, StatusCode::OK);
    }
    assert_eq!(chat(&s, &key, json!("vip")).await.0, StatusCode::TOO_MANY_REQUESTS);

    adm(&s, Method::PUT, "/admin/keys/api/users/nakal", json!({"active": false})).await;
    let (st, v, _) = chat(&s, &key, json!("nakal")).await;
    assert_eq!((st, kode(&v)), (StatusCode::FORBIDDEN, "user_blocked"));
    // Dicabut lagi pengaturannya: kembali ke bawaan.
    let (st, _) = adm(&s, Method::DELETE, "/admin/keys/api/users/nakal", Value::Null).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(chat(&s, &key, json!("nakal")).await.0, StatusCode::OK);
}

#[tokio::test]
async fn key_wajib_user_menolak_request_tanpa_label() {
    let t = Tmp::baru("wajib");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&base, &t);
    let key = key_baru(&s, json!({"name": "api", "user_required": true})).await;
    for tanpa in [Value::Null, json!("")] {
        let (st, v, _) = chat(&s, &key, tanpa).await;
        assert_eq!((st, kode(&v)), (StatusCode::BAD_REQUEST, "user_required"));
    }
    assert_eq!(chat(&s, &key, json!("mall-a")).await.0, StatusCode::OK);

    // Key biasa tetap boleh tanpa user.
    let biasa = key_baru(&s, json!({"name": "internal"})).await;
    assert_eq!(chat(&s, &biasa, Value::Null).await.0, StatusCode::OK);
}

#[tokio::test]
async fn label_user_tidak_valid_ditolak_400() {
    let t = Tmp::baru("invalid");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&base, &t);
    let key = key_baru(&s, json!({"name": "api"})).await;
    for salah in [json!(5), json!({"id": 1}), json!("ada spasi"), json!("x".repeat(65)), json!("baris\nbaru")] {
        let (st, v, _) = chat(&s, &key, salah.clone()).await;
        assert_eq!((st, kode(&v)), (StatusCode::BAD_REQUEST, "invalid_user"), "{salah}");
    }
    for sah in ["mall-a", "client:42", "ops@tim", "a.b_c"] {
        assert_eq!(chat(&s, &key, json!(sah)).await.0, StatusCode::OK, "{sah}");
    }
}

#[tokio::test]
async fn estimasi_tpm_client_dikembalikan_saat_upstream_gagal() {
    let t = Tmp::baru("refund");
    let gagal =
        Router::new().route("/v1/chat/completions", post(|| async { (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": "x"}))) }));
    let base = jalankan_upstream(gagal).await;
    let s = sistem(&base, &t);
    let key = key_baru(&s, json!({"name": "api", "user_tpm": 1000})).await;
    let (st, _, _) = chat(&s, &key, json!("mall-a")).await;
    assert!(st.is_server_error(), "{st}");
    let (_, v) = info(&s, &key, "?user=mall-a").await;
    assert_eq!(v["user"]["remaining"]["tpm"], 1000, "{v}");
}

// ---------- key/info ----------

#[tokio::test]
async fn key_info_melaporkan_aturan_dan_sisa_client() {
    let t = Tmp::baru("info");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&base, &t);
    let key = key_baru(&s, json!({"name": "api", "user_rpm": 5, "user_required": true})).await;
    let (_, v) = info(&s, &key, "").await;
    assert_eq!((v["user_required"].as_bool(), v["user_limits"]["rpm"].as_u64()), (Some(true), Some(5)));
    assert!(v.get("user").is_none());

    chat(&s, &key, json!("ops@tim")).await;
    // '@' dikirim ter-encode oleh klien HTTP pada umumnya.
    let (st, v) = info(&s, &key, "?user=ops%40tim").await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["user"]["name"], "ops@tim");
    assert_eq!(v["user"]["active"], true);
    assert_eq!(v["user"]["limits"], json!({"rpm": 5, "tpm": null}));
    assert_eq!(v["user"]["remaining"]["rpm"], 4);

    adm(&s, Method::PUT, "/admin/keys/api/users/diblok", json!({"active": false})).await;
    assert_eq!(info(&s, &key, "?user=diblok").await.1["user"]["active"], false);

    let (st, v) = info(&s, &key, "?user=ada%20spasi").await;
    assert_eq!((st, kode(&v)), (StatusCode::BAD_REQUEST, "invalid_user"));
}

// ---------- API admin ----------

#[tokio::test]
async fn admin_mengelola_aturan_dan_daftar_client() {
    let t = Tmp::baru("admin");
    let s = sistem("http://127.0.0.1:1/v1", &t);
    key_baru(&s, json!({"name": "api"})).await;

    let (st, v) = adm(&s, Method::PATCH, "/admin/keys/api", json!({"user_rpm": 10, "user_required": true})).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(
        (v["info"]["user_rpm"].as_u64(), v["info"]["user_tpm"].clone(), v["info"]["user_required"].as_bool()),
        (Some(10), Value::Null, Some(true))
    );
    // Field lain tidak menyentuh aturan client; null menghapus batas bawaan client.
    let (_, v) = adm(&s, Method::PATCH, "/admin/keys/api", json!({"rpm": 50})).await;
    assert_eq!(v["info"]["user_rpm"], 10);
    let (_, v) = adm(&s, Method::PATCH, "/admin/keys/api", json!({"user_rpm": null})).await;
    assert_eq!((v["info"]["user_rpm"].clone(), v["info"]["user_required"].as_bool()), (Value::Null, Some(true)));

    adm(&s, Method::PUT, "/admin/keys/api/users/zeta", json!({"tpm": 900})).await;
    adm(&s, Method::PUT, "/admin/keys/api/users/alfa", json!({})).await;
    let (st, v) = adm(&s, Method::GET, "/admin/keys/api/users", Value::Null).await;
    assert_eq!(st, StatusCode::OK);
    let nama: Vec<&str> = v["users"].as_array().unwrap().iter().map(|u| u["user"].as_str().unwrap()).collect();
    assert_eq!(nama, ["alfa", "zeta"]);
    assert_eq!(v["users"][1]["tpm_efektif"], 900);

    // PUT mengganti seluruh isi (rpm tidak dikirim = ikut bawaan).
    let (_, v) = adm(&s, Method::PUT, "/admin/keys/api/users/zeta", json!({"rpm": 2})).await;
    assert_eq!((v["user"]["rpm"].as_u64(), v["user"]["tpm"].clone()), (Some(2), Value::Null));

    for (metode, uri, body, status, kode_harap) in [
        (Method::GET, "/admin/keys/tidak-ada/users", Value::Null, StatusCode::NOT_FOUND, "key_not_found"),
        (Method::PUT, "/admin/keys/tidak-ada/users/x", json!({}), StatusCode::NOT_FOUND, "key_not_found"),
        (Method::PUT, "/admin/keys/api/users/x", json!({"rpm": 0}), StatusCode::BAD_REQUEST, "invalid_limit"),
        (Method::PUT, "/admin/keys/api/users/x", json!({"lain": 1}), StatusCode::BAD_REQUEST, "invalid_body"),
        (Method::PUT, "/admin/keys/api/users/ada%20spasi", json!({}), StatusCode::BAD_REQUEST, "invalid_user"),
        (Method::DELETE, "/admin/keys/api/users/belum-ada", Value::Null, StatusCode::NOT_FOUND, "user_not_found"),
        (Method::PATCH, "/admin/keys/api", json!({"user_tpm": 0}), StatusCode::BAD_REQUEST, "invalid_limit"),
    ] {
        let (st, v) = adm(&s, metode.clone(), uri, body).await;
        assert_eq!((st, kode(&v)), (status, kode_harap), "{metode} {uri}: {v}");
    }

    // Menghapus key ikut menghapus pengaturan client-nya: key baru bernama sama mulai bersih.
    adm(&s, Method::DELETE, "/admin/keys/api", Value::Null).await;
    key_baru(&s, json!({"name": "api"})).await;
    let (_, v) = adm(&s, Method::GET, "/admin/keys/api/users", Value::Null).await;
    assert_eq!(v["users"], json!([]));
}

// ---------- statistik ----------

#[tokio::test]
async fn statistik_per_client() {
    let t = Tmp::baru("stats");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&base, &t);
    let key = key_baru(&s, json!({"name": "api"})).await;
    chat(&s, &key, json!("mall-a")).await;
    chat(&s, &key, json!("mall-a")).await;
    chat(&s, &key, json!("mall-b")).await;
    chat(&s, &key, Value::Null).await;
    s.stat.tutup();

    let baris = s.stat.ringkasan(0, i64::MAX, nigate::stats::Kelompok::User).unwrap();
    let peta: std::collections::HashMap<_, _> = baris.iter().map(|b| (b.kelompok.as_str(), b.request)).collect();
    assert_eq!(peta, [("api/mall-a", 2), ("api/mall-b", 1), ("api/-", 1)].into_iter().collect());
}

// ---------- limiter ----------

#[test]
fn ember_client_yang_penuh_dibuang_saat_peta_membesar() {
    let l = Limiter::default();
    let t0 = Instant::now();
    let b = |i: usize| Batas { subjek: Subjek::Client(1, Arc::from(format!("c{i}").as_str())), rpm: Some(10), tpm: None };
    for i in 0..50_000 {
        l.coba_semua(&[b(i)], 1, t0).unwrap();
    }
    assert_eq!(l.jumlah_ember(), 50_000);
    // Semenit kemudian semua ember sudah penuh lagi: client baru memicu penyapuan tanpa mengubah perilaku.
    l.coba_semua(&[b(999_999)], 1, t0 + Duration::from_secs(61)).unwrap();
    assert!(l.jumlah_ember() < 10, "{}", l.jumlah_ember());
    // Ember yang masih terpakai (belum penuh) tidak dibuang.
    let l = Limiter::default();
    for i in 0..50_000 {
        l.coba_semua(&[b(i)], 1, t0).unwrap();
    }
    l.coba_semua(&[b(999_999)], 1, t0 + Duration::from_secs(1)).unwrap();
    assert_eq!(l.jumlah_ember(), 50_001);
}

// ---------- penyimpanan ----------

#[test]
fn database_lama_mendapat_kolom_dan_tabel_client_tanpa_naik_versi() {
    let t = Tmp::baru("migrasi");
    {
        let c = rusqlite::Connection::open(t.path()).unwrap();
        c.execute_batch(
            "CREATE TABLE api_keys (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, key_hash TEXT NOT NULL UNIQUE,
                 key_prefix TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, rpm INTEGER, tpm INTEGER);
             INSERT INTO api_keys (name, key_hash, key_prefix, active, created_at) VALUES ('lama', 'ab', 'ngk_ab', 1, 1);
             PRAGMA user_version = 2;",
        )
        .unwrap();
    }
    let store = KeyStore::open(t.path()).unwrap();
    let k = store.get("lama").unwrap().unwrap();
    assert_eq!((k.user_rpm, k.user_tpm, k.user_required), (None, None, false));
    assert!(store.set_client("lama", "mall-a", Some(5), None, true).unwrap().is_some());
    drop(store);
    let c = rusqlite::Connection::open(t.path()).unwrap();
    assert_eq!(c.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0)).unwrap(), 2);
    drop(c);
    let store = KeyStore::open(t.path()).unwrap();
    assert_eq!(store.list_clients("lama").unwrap().unwrap()[0].rpm, Some(5));
}

#[test]
fn database_statistik_lama_mendapat_kolom_end_user_tanpa_naik_versi() {
    let t = Tmp::baru("migrasi-stats");
    {
        let c = rusqlite::Connection::open(t.path()).unwrap();
        c.execute_batch(
            "CREATE TABLE requests (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, key_id INTEGER NOT NULL, key_name TEXT NOT NULL,
                 alias TEXT, upstream TEXT, status INTEGER NOT NULL, hasil TEXT NOT NULL, kode_galat TEXT, token_masuk INTEGER,
                 token_keluar INTEGER, latensi_ms INTEGER NOT NULL, percobaan INTEGER NOT NULL,
                 temuan_masuk INTEGER NOT NULL DEFAULT 0, temuan_keluar INTEGER NOT NULL DEFAULT 0, jenis_temuan TEXT);
             PRAGMA user_version = 2;",
        )
        .unwrap();
    }
    let st = Statistik::buka(t.path(), 30).unwrap();
    st.tutup();
    drop(st);
    let c = rusqlite::Connection::open(t.path()).unwrap();
    assert_eq!(c.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0)).unwrap(), 2);
    let ada: bool =
        c.query_row("SELECT COUNT(*) > 0 FROM pragma_table_info('requests') WHERE name = 'end_user'", [], |r| r.get(0)).unwrap();
    assert!(ada);
}

// ---------- header X-Nigate-User ----------

async fn chat_header(s: &Sistem, key: &str, header: &str, user_body: Option<&str>) -> (StatusCode, Value) {
    let mut body = json!({"model": "m1", "messages": [{"role": "user", "content": "halo"}]});
    if let Some(u) = user_body {
        body["user"] = json!(u);
    }
    let req = Request::builder()
        .method(Method::POST)
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .header("authorization", format!("Bearer {key}"))
        .header("x-nigate-user", header)
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = s.data.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

#[tokio::test]
async fn label_client_lewat_header_setara_field_user() {
    let t = Tmp::baru("header");
    let rekam = Rekam::default();
    let base = jalankan_upstream(upstream_normal(rekam.clone())).await;
    let s = sistem(&base, &t);
    let key = key_baru(&s, json!({"name": "api", "user_rpm": 1, "user_required": true})).await;

    assert_eq!(chat_header(&s, &key, "mall-a", None).await.0, StatusCode::OK);
    // Ember yang sama dengan label dari body.
    let (st, v, _) = chat(&s, &key, json!("mall-a")).await;
    assert_eq!((st, v["error"]["param"].as_str()), (StatusCode::TOO_MANY_REQUESTS, Some("user")));
    // Header dan body sama: boleh. Berbeda: ditolak.
    assert_eq!(chat_header(&s, &key, "mall-b", Some("mall-b")).await.0, StatusCode::OK);
    let (st, v) = chat_header(&s, &key, "mall-c", Some("mall-d")).await;
    assert_eq!((st, kode(&v)), (StatusCode::BAD_REQUEST, "invalid_user"));
    // Header kosong = tidak ada label.
    let (st, v) = chat_header(&s, &key, "  ", None).await;
    assert_eq!((st, kode(&v)), (StatusCode::BAD_REQUEST, "user_required"));
    let (st, v) = chat_header(&s, &key, "ada spasi", None).await;
    assert_eq!((st, kode(&v)), (StatusCode::BAD_REQUEST, "invalid_user"));
    // Header label tidak diteruskan ke provider.
    s.stat.tutup();
    let baris = s.stat.ringkasan(0, i64::MAX, nigate::stats::Kelompok::User).unwrap();
    assert!(baris.iter().any(|b| b.kelompok == "api/mall-a" && b.request == 2), "{baris:?}");
    let _ = rekam;
}
