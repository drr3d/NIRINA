//! Metadata per key dan `GET /v1/key/info`: dipakai aplikasi di depan gateway (mis. API NIRINA) untuk mengetahui milik siapa
//! sebuah key (label `client_id`, dst.) tanpa gateway membaca database aplikasi itu.

use std::path::PathBuf;
use std::sync::Arc;

use axum::{
    Router,
    body::Body,
    http::{HeaderMap, Method, Request, StatusCode},
};
use http_body_util::BodyExt;
use nigate::{AppState, admin::admin_app, app, config::Config, keys::KeyStore, stats::Statistik};
use serde_json::{Value, json};
use tower::ServiceExt;

mod common;
use common::{Rekam, jalankan_upstream, upstream_normal};

const ADMIN: &str = "token-admin-uji-yang-cukup-panjang-123456";

struct Tmp(PathBuf);

impl Tmp {
    fn baru(nama: &str) -> Self {
        let p = std::env::temp_dir().join(format!("nigate-keyinfo-{nama}-{}", std::process::id()));
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
}

fn sistem(base: &str, ekstra: &str, stats: &Tmp, keys: KeyStore) -> Sistem {
    let toml = format!(
        "[resilience]\nretry_backoff_ms = 5\n{ekstra}\n\
         [[model]]\nalias = \"m1\"\n[[model.upstream]]\nname = \"utama\"\nbase_url = \"{base}\"\nmodel = \"asli\"\napi_key_env = \"K\"\n"
    );
    let env = |n: &str| match n {
        "NIGATE_ADMIN_TOKEN" => Some(ADMIN.to_string()),
        "K" => Some("kunci-provider".to_string()),
        _ => None,
    };
    let cfg = Config::from_toml_str(&toml, &env).unwrap();
    let stat = Arc::new(Statistik::buka(stats.path(), 30).unwrap());
    let state = AppState::new(cfg, Arc::new(keys)).unwrap().dengan_statistik(stat);
    Sistem { data: app(state.clone()), admin: admin_app(state) }
}

async fn panggil(r: &Router, metode: Method, uri: &str, token: Option<&str>, body: Option<&str>) -> (StatusCode, Value, HeaderMap) {
    let mut rb = Request::builder().method(metode).uri(uri).header("content-type", "application/json");
    if let Some(t) = token {
        rb = rb.header("authorization", format!("Bearer {t}"));
    }
    let resp = r.clone().oneshot(rb.body(Body::from(body.unwrap_or("").to_string())).unwrap()).await.unwrap();
    let (status, headers) = (resp.status(), resp.headers().clone());
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null), headers)
}

async fn adm(s: &Sistem, metode: Method, uri: &str, body: Value) -> (StatusCode, Value) {
    let body = (!body.is_null()).then(|| body.to_string());
    let (st, v, _) = panggil(&s.admin, metode, uri, Some(ADMIN), body.as_deref()).await;
    (st, v)
}

async fn info(s: &Sistem, key: Option<&str>) -> (StatusCode, Value, HeaderMap) {
    panggil(&s.data, Method::GET, "/v1/key/info", key, None).await
}

async fn chat(s: &Sistem, key: &str) -> StatusCode {
    let body = json!({"model": "m1", "messages": [{"role": "user", "content": "halo"}]}).to_string();
    panggil(&s.data, Method::POST, "/v1/chat/completions", Some(key), Some(&body)).await.0
}

fn kode(v: &Value) -> &str {
    v["error"]["code"].as_str().unwrap_or("")
}

// ---------- metadata lewat API admin ----------

#[tokio::test]
async fn metadata_disimpan_saat_buat_lalu_bisa_diganti_dan_dihapus() {
    let t = Tmp::baru("meta-admin");
    let s = sistem("http://127.0.0.1:1/v1", "", &t, KeyStore::open_memory().unwrap());

    let (st, v) =
        adm(&s, Method::POST, "/admin/keys", json!({"name": "api-mall-a", "metadata": {"client_id": "mall-a", "tier": "gold"}})).await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    assert_eq!(v["info"]["metadata"], json!({"client_id": "mall-a", "tier": "gold"}));

    let (_, v) = adm(&s, Method::GET, "/admin/keys", Value::Null).await;
    assert_eq!(v["keys"][0]["metadata"]["client_id"], "mall-a");

    // PATCH metadata mengganti seluruh isi (bukan menggabung).
    let (st, v) = adm(&s, Method::PATCH, "/admin/keys/api-mall-a", json!({"metadata": {"client_id": "mall-b"}})).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["info"]["metadata"], json!({"client_id": "mall-b"}));

    // Field lain tidak menyentuh metadata.
    let (_, v) = adm(&s, Method::PATCH, "/admin/keys/api-mall-a", json!({"rpm": 10})).await;
    assert_eq!(v["info"]["metadata"], json!({"client_id": "mall-b"}));

    // null = kosongkan.
    let (_, v) = adm(&s, Method::PATCH, "/admin/keys/api-mall-a", json!({"metadata": null})).await;
    assert_eq!(v["info"]["metadata"], json!({}));
}

#[tokio::test]
async fn key_tanpa_metadata_punya_objek_kosong() {
    let t = Tmp::baru("meta-kosong");
    let s = sistem("http://127.0.0.1:1/v1", "", &t, KeyStore::open_memory().unwrap());
    let (_, v) = adm(&s, Method::POST, "/admin/keys", json!({"name": "polos"})).await;
    assert_eq!(v["info"]["metadata"], json!({}));
}

#[tokio::test]
async fn metadata_tidak_valid_ditolak_400() {
    let t = Tmp::baru("meta-invalid");
    let s = sistem("http://127.0.0.1:1/v1", "", &t, KeyStore::open_memory().unwrap());
    let banyak: serde_json::Map<String, Value> = (0..17).map(|i| (format!("k{i}"), json!("v"))).collect();
    let salah = [
        json!({"client id": "spasi di nama"}),
        json!({"": "nama kosong"}),
        json!({"client_id": 5}),
        json!({"client_id": "x".repeat(257)}),
        json!({"client_id": "ada\nbaris baru"}),
        json!({"a".repeat(65): "nama kepanjangan"}),
        Value::Object(banyak),
        json!(["bukan", "objek"]),
    ];
    for m in salah {
        let (st, v) = adm(&s, Method::POST, "/admin/keys", json!({"name": "k", "metadata": m})).await;
        assert_eq!((st, kode(&v)), (StatusCode::BAD_REQUEST, "invalid_metadata"), "{m}");
    }
    // Penolakan tidak meninggalkan key setengah jadi.
    let (_, v) = adm(&s, Method::GET, "/admin/keys", Value::Null).await;
    assert_eq!(v["keys"], json!([]));

    adm(&s, Method::POST, "/admin/keys", json!({"name": "ada"})).await;
    let (st, v) = adm(&s, Method::PATCH, "/admin/keys/ada", json!({"metadata": {"bad key": "x"}})).await;
    assert_eq!((st, kode(&v)), (StatusCode::BAD_REQUEST, "invalid_metadata"));
}

// ---------- GET /v1/key/info ----------

#[tokio::test]
async fn key_info_mengembalikan_identitas_metadata_dan_batas() {
    let t = Tmp::baru("info");
    let s = sistem("http://127.0.0.1:1/v1", "[limits]\ndefault_rpm = 120\n", &t, KeyStore::open_memory().unwrap());
    let (_, v) =
        adm(&s, Method::POST, "/admin/keys", json!({"name": "api-mall-a", "tpm": 5000, "metadata": {"client_id": "mall-a"}})).await;
    let key = v["key"].as_str().unwrap().to_string();

    let (st, v, h) = info(&s, Some(&key)).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(h["cache-control"], "no-store");
    assert_eq!(v["name"], "api-mall-a");
    assert_eq!(v["active"], true);
    assert_eq!(v["metadata"], json!({"client_id": "mall-a"}));
    // Batas efektif: rpm dari default config, tpm dari key.
    assert_eq!(v["limits"], json!({"rpm": 120, "tpm": 5000}));
    assert_eq!(v["remaining"], json!({"rpm": 120, "tpm": 5000}));
    // Tidak pernah membocorkan key asli atau hash-nya.
    let teks = v.to_string();
    assert!(!teks.contains(&key) && !teks.contains("hash"), "{teks}");
}

#[tokio::test]
async fn key_info_menolak_key_kosong_salah_atau_dicabut() {
    let t = Tmp::baru("info-tolak");
    let s = sistem("http://127.0.0.1:1/v1", "", &t, KeyStore::open_memory().unwrap());
    let (_, v) = adm(&s, Method::POST, "/admin/keys", json!({"name": "a"})).await;
    let key = v["key"].as_str().unwrap().to_string();

    let (st, v, _) = info(&s, None).await;
    assert_eq!((st, kode(&v)), (StatusCode::UNAUTHORIZED, "missing_api_key"));
    let (st, v, _) = info(&s, Some("ngk_salah")).await;
    assert_eq!((st, kode(&v)), (StatusCode::UNAUTHORIZED, "invalid_api_key"));

    adm(&s, Method::PATCH, "/admin/keys/a", json!({"active": false})).await;
    let (st, v, _) = info(&s, Some(&key)).await;
    assert_eq!((st, kode(&v)), (StatusCode::UNAUTHORIZED, "invalid_api_key"));
}

#[tokio::test]
async fn key_info_tidak_memakai_jatah_dan_sisa_berkurang_setelah_chat() {
    let t = Tmp::baru("info-jatah");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&base, "", &t, KeyStore::open_memory().unwrap());
    let (_, v) = adm(&s, Method::POST, "/admin/keys", json!({"name": "a", "rpm": 2})).await;
    let key = v["key"].as_str().unwrap().to_string();

    for _ in 0..5 {
        assert_eq!(info(&s, Some(&key)).await.0, StatusCode::OK);
    }
    assert_eq!(chat(&s, &key).await, StatusCode::OK);
    let (_, v, _) = info(&s, Some(&key)).await;
    assert_eq!(v["remaining"]["rpm"], 1);
    assert_eq!(chat(&s, &key).await, StatusCode::OK);
    let (_, v, _) = info(&s, Some(&key)).await;
    assert_eq!(v["remaining"]["rpm"], 0);
    // Jatah habis: chat ditolak, tetapi key/info tetap menjawab (supaya aplikasi bisa menampilkan sisa kuota).
    assert_eq!(chat(&s, &key).await, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(info(&s, Some(&key)).await.0, StatusCode::OK);
}

#[tokio::test]
async fn perubahan_metadata_langsung_terlihat_di_key_info() {
    let t = Tmp::baru("info-segar");
    let s = sistem("http://127.0.0.1:1/v1", "", &t, KeyStore::open_memory().unwrap());
    let (_, v) = adm(&s, Method::POST, "/admin/keys", json!({"name": "a", "metadata": {"client_id": "lama"}})).await;
    let key = v["key"].as_str().unwrap().to_string();
    assert_eq!(info(&s, Some(&key)).await.1["metadata"]["client_id"], "lama");
    adm(&s, Method::PATCH, "/admin/keys/a", json!({"metadata": {"client_id": "baru"}})).await;
    assert_eq!(info(&s, Some(&key)).await.1["metadata"]["client_id"], "baru");
}

#[tokio::test]
async fn tanpa_batas_sisa_null() {
    let t = Tmp::baru("info-tanpa-batas");
    let s = sistem("http://127.0.0.1:1/v1", "", &t, KeyStore::open_memory().unwrap());
    let (_, v) = adm(&s, Method::POST, "/admin/keys", json!({"name": "a"})).await;
    let (_, v, _) = info(&s, v["key"].as_str()).await;
    assert_eq!(v["limits"], json!({"rpm": null, "tpm": null}));
    assert_eq!(v["remaining"], json!({"rpm": null, "tpm": null}));
}

// ---------- penyimpanan ----------

#[test]
fn metadata_bertahan_setelah_dibuka_ulang() {
    let t = Tmp::baru("persist");
    let store = KeyStore::open(t.path()).unwrap();
    let (_, token) = store.create("a").unwrap();
    let meta = [("client_id".to_string(), "mall-a".to_string())].into_iter().collect();
    assert!(store.set_metadata("a", &meta).unwrap());
    drop(store);
    let store = KeyStore::open(t.path()).unwrap();
    assert_eq!(store.get("a").unwrap().unwrap().metadata, meta);
    assert_eq!(store.authenticate(&token).unwrap().metadata, meta);
}

#[test]
fn database_lama_tanpa_kolom_metadata_tetap_terbuka_dan_versi_skema_tidak_naik() {
    // Database rilis sebelumnya (skema versi 2, tanpa kolom metadata). Versi skema sengaja tidak dinaikkan supaya rilis lama
    // tetap bisa membuka database ini lagi (rollback TAG).
    let t = Tmp::baru("lama");
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
    assert!(store.get("lama").unwrap().unwrap().metadata.is_empty());
    let meta = [("client_id".to_string(), "x".to_string())].into_iter().collect();
    assert!(store.set_metadata("lama", &meta).unwrap());
    drop(store);
    let c = rusqlite::Connection::open(t.path()).unwrap();
    let v: i64 = c.query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
    assert_eq!(v, 2);
    // Dibuka kedua kali (kolom sudah ada) tidak gagal.
    drop(c);
    assert_eq!(KeyStore::open(t.path()).unwrap().get("lama").unwrap().unwrap().metadata, meta);
}
