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
const KUNCI_PROVIDER: &str = "kunci-provider-rahasia-jangan-bocor";

struct Tmp(PathBuf);

impl Tmp {
    fn baru(nama: &str) -> Self {
        let p = std::env::temp_dir().join(format!("nigate-admin-{nama}-{}", std::process::id()));
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
    state: AppState,
    stat: Arc<Statistik>,
}

fn toml_dasar(base: &str, ekstra: &str) -> String {
    format!(
        "[resilience]\nretry_backoff_ms = 5\ncooldown_secs = 60\n{ekstra}\n\
         [[model]]\nalias = \"m1\"\n[[model.upstream]]\nname = \"utama\"\nbase_url = \"{base}\"\nmodel = \"asli\"\napi_key_env = \"K\"\n"
    )
}

fn env_uji(n: &str) -> Option<String> {
    match n {
        "NIGATE_ADMIN_TOKEN" => Some(ADMIN.to_string()),
        "K" => Some(KUNCI_PROVIDER.to_string()),
        _ => None,
    }
}

fn sistem(toml: &str, stats: &Tmp) -> Sistem {
    let cfg = Config::from_toml_str(toml, &env_uji).unwrap();
    let stat = Arc::new(Statistik::buka(stats.path(), 30).unwrap());
    let state = AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap())).unwrap().dengan_statistik(stat.clone());
    Sistem { data: app(state.clone()), admin: admin_app(state.clone()), state, stat }
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

async fn adm(s: &Sistem, metode: Method, uri: &str, body: Option<&str>) -> (StatusCode, Value) {
    let (st, v, _) = panggil(&s.admin, metode, uri, Some(ADMIN), body).await;
    (st, v)
}

async fn chat(s: &Sistem, key: &str, isi: &str) -> StatusCode {
    let body = json!({"model": "m1", "messages": [{"role": "user", "content": isi}]}).to_string();
    panggil(&s.data, Method::POST, "/v1/chat/completions", Some(key), Some(&body)).await.0
}

async fn baru_key(s: &Sistem, nama: &str) -> String {
    let (st, v) = adm(s, Method::POST, "/admin/keys", Some(&json!({"name": nama}).to_string())).await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    v["key"].as_str().unwrap().to_string()
}

// ---------- auth ----------

#[tokio::test]
async fn tanpa_atau_salah_token_ditolak_dan_respons_tidak_di_cache() {
    let t = Tmp::baru("auth");
    let s = sistem(&toml_dasar("http://127.0.0.1:1/v1", ""), &t);
    let (st, v, h) = panggil(&s.admin, Method::GET, "/admin/health", None, None).await;
    assert_eq!((st, v["error"]["code"].as_str()), (StatusCode::UNAUTHORIZED, Some("invalid_admin_token")));
    assert_eq!(h["cache-control"], "no-store");
    let (st, _, _) = panggil(&s.admin, Method::GET, "/admin/health", Some("token-salah-tapi-panjangnya-mirip-123"), None).await;
    assert_eq!(st, StatusCode::UNAUTHORIZED);
    let (st, _, h) = panggil(&s.admin, Method::GET, "/admin/health", Some(ADMIN), None).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(h["cache-control"], "no-store");
}

#[tokio::test]
async fn tanpa_token_terkonfigurasi_admin_menolak_semuanya() {
    let t = Tmp::baru("notoken");
    let cfg = Config::from_toml_str(&toml_dasar("http://127.0.0.1:1/v1", ""), &|n| (n == "K").then(|| KUNCI_PROVIDER.to_string())).unwrap();
    assert!(cfg.admin_token.is_none());
    let state = AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap()))
        .unwrap()
        .dengan_statistik(Arc::new(Statistik::buka(t.path(), 30).unwrap()));
    let r = admin_app(state);
    for tok in [None, Some(""), Some("apa-saja-token-yang-panjang-sekali-ok")] {
        assert_eq!(panggil(&r, Method::GET, "/admin/health", tok, None).await.0, StatusCode::UNAUTHORIZED);
    }
}

#[test]
fn token_admin_terlalu_pendek_ditolak_saat_config_dimuat() {
    let galat = Config::from_toml_str("", &|n| (n == "NIGATE_ADMIN_TOKEN").then(|| "pendek".to_string())).err().unwrap();
    assert!(format!("{galat:#}").contains("terlalu pendek"));
}

#[tokio::test]
async fn health_melaporkan_keadaan() {
    let t = Tmp::baru("health");
    let s = sistem(&toml_dasar("http://127.0.0.1:1/v1", ""), &t);
    let (st, v) = adm(&s, Method::GET, "/admin/health", None).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(
        (v["status"].as_str(), v["jumlah_model"].as_u64(), v["auth_required"].as_bool(), v["guardrail_aktif"].as_bool()),
        (Some("ok"), Some(1), Some(true), Some(true))
    );
    assert_eq!(v["reload_tersedia"], false);
}

// ---------- key ----------

#[tokio::test]
async fn key_dibuat_lewat_admin_langsung_bisa_dipakai_di_jalur_chat() {
    let t = Tmp::baru("key-buat");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&toml_dasar(&base, ""), &t);

    let (st, v) = adm(&s, Method::POST, "/admin/keys", Some(r#"{"name":"tim-a","rpm":60,"tpm":100000}"#)).await;
    assert_eq!(st, StatusCode::CREATED);
    let key = v["key"].as_str().unwrap().to_string();
    assert!(key.starts_with("ngk_") && key.len() == 68);
    assert_eq!((v["info"]["rpm"].as_u64(), v["info"]["tpm"].as_u64(), v["info"]["active"].as_bool()), (Some(60), Some(100000), Some(true)));
    assert!(v["info"].get("key_hash").is_none() && !v["info"].to_string().contains(&key), "info tidak memuat key/hash");

    assert_eq!(chat(&s, &key, "halo").await, StatusCode::OK);

    let (st, v) = adm(&s, Method::GET, "/admin/keys", None).await;
    assert_eq!(st, StatusCode::OK);
    let daftar = v["keys"].as_array().unwrap();
    assert_eq!(daftar.len(), 1);
    assert!(!v.to_string().contains(&key), "daftar tidak boleh memuat key asli");
}

#[tokio::test]
async fn buat_key_menolak_input_buruk() {
    let t = Tmp::baru("key-buruk");
    let s = sistem(&toml_dasar("http://127.0.0.1:1/v1", ""), &t);
    baru_key(&s, "ada").await;
    let cek = |body: &'static str, status: StatusCode, kode: &'static str| {
        let s = &s;
        async move {
            let (st, v) = adm(s, Method::POST, "/admin/keys", Some(body)).await;
            assert_eq!((st, v["error"]["code"].as_str()), (status, Some(kode)), "body: {body}");
        }
    };
    cek(r#"{"name":"ada"}"#, StatusCode::CONFLICT, "name_taken").await;
    cek(r#"{"name":"nama salah!"}"#, StatusCode::BAD_REQUEST, "invalid_name").await;
    cek(r#"{"name":"x","rpm":0}"#, StatusCode::BAD_REQUEST, "invalid_limit").await;
    cek(r#"{"name":"x","ngawur":1}"#, StatusCode::BAD_REQUEST, "invalid_body").await;
    cek("bukan json", StatusCode::BAD_REQUEST, "invalid_body").await;
    assert_eq!(
        adm(&s, Method::GET, "/admin/keys", None).await.1["keys"].as_array().unwrap().len(),
        1,
        "yang gagal tidak meninggalkan key setengah jadi"
    );
}

#[tokio::test]
async fn patch_key_mencabut_mengaktifkan_dan_mengatur_batas_dengan_semantik_null() {
    let t = Tmp::baru("key-patch");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&toml_dasar(&base, "[limits]\ndefault_rpm = 1000"), &t);
    let key = baru_key(&s, "tim-a").await;

    let (st, v) = adm(&s, Method::PATCH, "/admin/keys/tim-a", Some(r#"{"active":false}"#)).await;
    assert_eq!((st, v["info"]["active"].as_bool()), (StatusCode::OK, Some(false)));
    assert_eq!(chat(&s, &key, "x").await, StatusCode::UNAUTHORIZED, "pencabutan langsung berlaku");
    adm(&s, Method::PATCH, "/admin/keys/tim-a", Some(r#"{"active":true}"#)).await;
    assert_eq!(chat(&s, &key, "x").await, StatusCode::OK);

    let (_, v) = adm(&s, Method::PATCH, "/admin/keys/tim-a", Some(r#"{"rpm":5,"tpm":900}"#)).await;
    assert_eq!((v["info"]["rpm"].as_u64(), v["info"]["tpm"].as_u64()), (Some(5), Some(900)));
    let (_, v) = adm(&s, Method::PATCH, "/admin/keys/tim-a", Some(r#"{"tpm":null}"#)).await;
    assert_eq!((v["info"]["rpm"].as_u64(), v["info"]["tpm"].as_u64()), (Some(5), None), "null menghapus tpm, rpm dibiarkan");
    let (_, v) = adm(&s, Method::PATCH, "/admin/keys/tim-a", Some(r#"{"rpm":null}"#)).await;
    assert_eq!((v["info"]["rpm"].as_u64(), v["info"]["rpm_efektif"].as_u64()), (None, Some(1000)), "rpm_efektif ikut default config");

    let cek = |uri: &'static str, body: &'static str, status: StatusCode, kode: &'static str| {
        let s = &s;
        async move {
            let (st, v) = adm(s, Method::PATCH, uri, Some(body)).await;
            assert_eq!((st, v["error"]["code"].as_str()), (status, Some(kode)), "{uri} {body}");
        }
    };
    cek("/admin/keys/tim-a", "{}", StatusCode::BAD_REQUEST, "no_changes").await;
    cek("/admin/keys/tim-a", r#"{"rpm":0}"#, StatusCode::BAD_REQUEST, "invalid_limit").await;
    cek("/admin/keys/tim-a", r#"{"nama":"lain"}"#, StatusCode::BAD_REQUEST, "invalid_body").await;
    cek("/admin/keys/tidak-ada", r#"{"active":false}"#, StatusCode::NOT_FOUND, "key_not_found").await;
}

#[tokio::test]
async fn batas_yang_diatur_lewat_admin_langsung_ditegakkan_di_jalur_chat() {
    let t = Tmp::baru("key-limit");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&toml_dasar(&base, ""), &t);
    let key = baru_key(&s, "tim-a").await;
    adm(&s, Method::PATCH, "/admin/keys/tim-a", Some(r#"{"rpm":1}"#)).await;
    assert_eq!(chat(&s, &key, "1").await, StatusCode::OK);
    assert_eq!(chat(&s, &key, "2").await, StatusCode::TOO_MANY_REQUESTS);
}

#[tokio::test]
async fn delete_key_menghapus_permanen() {
    let t = Tmp::baru("key-hapus");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&toml_dasar(&base, ""), &t);
    let key = baru_key(&s, "tim-a").await;
    assert_eq!(adm(&s, Method::DELETE, "/admin/keys/tim-a", None).await.1["dihapus"], "tim-a");
    assert_eq!(chat(&s, &key, "x").await, StatusCode::UNAUTHORIZED);
    assert_eq!(adm(&s, Method::DELETE, "/admin/keys/tim-a", None).await.0, StatusCode::NOT_FOUND);
    assert_eq!(adm(&s, Method::GET, "/admin/keys", None).await.1["keys"].as_array().unwrap().len(), 0);
}

// ---------- upstream, config, statistik, guardrail ----------

#[tokio::test]
async fn status_upstream_menampilkan_cooldown_tanpa_membocorkan_rahasia() {
    let t = Tmp::baru("upstream");
    let s = sistem(&toml_dasar("http://pengguna:passw0rd-rahasia@127.0.0.1:1/v1", ""), &t);
    let key = baru_key(&s, "tim-a").await;

    let (_, v) = adm(&s, Method::GET, "/admin/upstreams", None).await;
    let u = &v["upstreams"][0];
    assert_eq!(
        (u["alias"].as_str(), u["name"].as_str(), u["dalam_cooldown"].as_bool(), u["terkonfigurasi"].as_bool()),
        (Some("m1"), Some("utama"), Some(false), Some(true))
    );

    assert_eq!(chat(&s, &key, "x").await, StatusCode::BAD_GATEWAY);
    let (_, v) = adm(&s, Method::GET, "/admin/upstreams", None).await;
    let u = &v["upstreams"][0];
    assert_eq!((u["dalam_cooldown"].as_bool(), u["gagal_beruntun"].as_u64()), (Some(true), Some(1)));
    assert!(u["sisa_cooldown_detik"].as_u64().unwrap() > 50);
    assert_eq!(u["url"], "http://127.0.0.1:1/v1", "userinfo dibuang dari URL");
    let mentah = v.to_string();
    assert!(!mentah.contains("passw0rd") && !mentah.contains(KUNCI_PROVIDER));
    assert_eq!(u["key_env"], "K", "hanya nama env, bukan nilainya");
}

#[tokio::test]
async fn config_efektif_tidak_memuat_rahasia() {
    let t = Tmp::baru("config");
    let s = sistem(&toml_dasar("http://127.0.0.1:1/v1", "[guardrail]\nmode = \"log_only\"\n[guardrail.aksi]\nprivate_key = \"block\""), &t);
    let (st, v) = adm(&s, Method::GET, "/admin/config", None).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!((v["guardrail"]["mode"].as_str(), v["guardrail"]["aksi"]["private_key"].as_str()), (Some("log_only"), Some("block")));
    assert_eq!(v["models"][0]["upstreams"][0]["name"], "utama");
    assert_eq!(v["resilience"]["cooldown_secs"], 60);
    let mentah = v.to_string();
    assert!(!mentah.contains(ADMIN) && !mentah.contains(KUNCI_PROVIDER));
}

#[tokio::test]
async fn stats_dan_kejadian_guardrail_lewat_admin() {
    let t = Tmp::baru("stats");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    let s = sistem(&toml_dasar(&base, ""), &t);
    let key = baru_key(&s, "tim-a").await;
    let rahasia = format!("{}_{}", "ghp", "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5");
    assert_eq!(chat(&s, &key, "halo").await, StatusCode::OK);
    assert_eq!(chat(&s, &key, &format!("pakai {rahasia}")).await, StatusCode::OK);
    s.stat.tutup();

    let (st, v) = adm(&s, Method::GET, "/admin/stats?jam=1&per=key", None).await;
    assert_eq!(st, StatusCode::OK);
    let b = &v["baris"][0];
    assert_eq!(
        (b["kelompok"].as_str(), b["request"].as_u64(), b["ok"].as_u64(), b["temuan"].as_u64()),
        (Some("tim-a"), Some(2), Some(2), Some(1))
    );
    for per in ["semua", "alias", "upstream", "hari", "jam"] {
        assert_eq!(adm(&s, Method::GET, &format!("/admin/stats?per={per}"), None).await.0, StatusCode::OK, "per={per}");
    }

    let (_, v) = adm(&s, Method::GET, "/admin/guardrail/events?jam=1", None).await;
    let k = v["kejadian"].as_array().unwrap();
    assert_eq!(k.len(), 1);
    assert_eq!(
        (k[0]["key_name"].as_str(), k[0]["jenis_temuan"].as_str(), k[0]["temuan_masuk"].as_u64()),
        (Some("tim-a"), Some("github_token"), Some(1))
    );
    assert!(!v.to_string().contains(&rahasia), "kejadian tidak memuat isi temuan");
}

#[tokio::test]
async fn parameter_query_divalidasi() {
    let t = Tmp::baru("query");
    let s = sistem(&toml_dasar("http://127.0.0.1:1/v1", ""), &t);
    for uri in [
        "/admin/stats?jam=0",
        "/admin/stats?jam=abc",
        "/admin/stats?jam=99999",
        "/admin/stats?per=ngawur",
        "/admin/guardrail/events?limit=0",
        "/admin/guardrail/events?limit=5000",
    ] {
        let (st, v) = adm(&s, Method::GET, uri, None).await;
        assert_eq!((st, v["error"]["code"].as_str()), (StatusCode::BAD_REQUEST, Some("invalid_query")), "{uri}");
    }
}

// ---------- reload ----------

#[tokio::test]
async fn reload_tanpa_file_config_dijawab_409() {
    let t = Tmp::baru("reload-409");
    let s = sistem(&toml_dasar("http://127.0.0.1:1/v1", ""), &t);
    let (st, v) = adm(&s, Method::POST, "/admin/reload", None).await;
    assert_eq!((st, v["error"]["code"].as_str()), (StatusCode::CONFLICT, Some("reload_unavailable")));
}

#[tokio::test]
async fn reload_menerapkan_model_baru_guardrail_baru_dan_menolak_config_rusak() {
    let cfg_file = Tmp::baru("reload.toml");
    let stats = Tmp::baru("reload-stats");
    let base = jalankan_upstream(upstream_normal(Rekam::default())).await;
    // Token admin dibaca dari env proses saat reload; nama unik supaya tidak bertabrakan dengan tes lain.
    unsafe { std::env::set_var("NIGATE_TES_ADMIN_RELOAD", ADMIN) };
    let tulis = |ekstra: &str, alias: &str| {
        std::fs::write(
            cfg_file.path(),
            format!(
                "[admin]\ntoken_env = \"NIGATE_TES_ADMIN_RELOAD\"\n{ekstra}\n[[model]]\nalias = \"{alias}\"\n[[model.upstream]]\nname = \"utama\"\nbase_url = \"{base}\"\nmodel = \"asli\"\n"
            ),
        )
        .unwrap();
    };
    tulis("", "m1");
    let cfg = Config::from_file(cfg_file.path()).unwrap();
    let stat = Arc::new(Statistik::buka(stats.path(), 30).unwrap());
    let state =
        AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap())).unwrap().dengan_statistik(stat).dengan_config_path(cfg_file.path());
    let s = Sistem { data: app(state.clone()), admin: admin_app(state.clone()), stat: state.statistik.clone(), state };
    let key = baru_key(&s, "tim-a").await;
    let secret = format!("kunci {}_{}", "ghp", "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5");

    assert_eq!(chat(&s, &key, &secret).await, StatusCode::OK, "awalnya redact: lolos");
    let (_, models, _) = panggil(&s.data, Method::GET, "/v1/models", Some(&key), None).await;
    assert_eq!(models["data"].as_array().unwrap().len(), 1);

    // config baru: alias baru + guardrail block + listen yang berubah (tidak boleh berlaku)
    tulis("[guardrail]\nmode = \"block\"\n[server]\nlisten = \"127.0.0.1:9999\"", "m2");
    let (st, v) = adm(&s, Method::POST, "/admin/reload", None).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["perlu_restart"], json!(["server.listen"]));
    assert_eq!(s.state.runtime().config.listen, "127.0.0.1:4000", "listen lama dipertahankan");

    let (_, models, _) = panggil(&s.data, Method::GET, "/v1/models", Some(&key), None).await;
    assert_eq!(models["data"][0]["id"], "m2");
    let body = json!({"model": "m2", "messages": [{"role": "user", "content": secret}]}).to_string();
    let (st, v, _) = panggil(&s.data, Method::POST, "/v1/chat/completions", Some(&key), Some(&body)).await;
    assert_eq!((st, v["error"]["code"].as_str()), (StatusCode::FORBIDDEN, Some("guardrail_blocked")), "guardrail baru langsung berlaku");

    // config rusak: ditolak seluruhnya, yang lama tetap jalan
    std::fs::write(cfg_file.path(), "[guardrail]\nmode = \"ngawur\"\n").unwrap();
    let (st, v) = adm(&s, Method::POST, "/admin/reload", None).await;
    assert_eq!((st, v["error"]["code"].as_str()), (StatusCode::BAD_REQUEST, Some("config_invalid")));
    let (_, models, _) = panggil(&s.data, Method::GET, "/v1/models", Some(&key), None).await;
    assert_eq!(models["data"][0]["id"], "m2", "config lama masih berjalan");
}
