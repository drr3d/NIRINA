use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

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
    keys::KeyStore,
    proxy::baca_usage,
    stats::{Kelompok, Rekaman, Statistik, klasifikasi, ringkasan_dari_file, sekarang_ms},
};
use serde_json::json;
use tower::ServiceExt;

mod common;
use common::jalankan_upstream;

struct DbTemp(PathBuf);

impl DbTemp {
    fn baru(nama: &str) -> Self {
        let p = std::env::temp_dir().join(format!("nigate-stats-{nama}-{}.db", std::process::id()));
        Self::hapus(&p);
        Self(p)
    }
    fn path(&self) -> &str {
        self.0.to_str().unwrap()
    }
    fn hapus(p: &std::path::Path) {
        for akhiran in ["", "-wal", "-shm", "-journal"] {
            let _ = std::fs::remove_file(format!("{}{akhiran}", p.display()));
        }
    }
}

impl Drop for DbTemp {
    fn drop(&mut self) {
        Self::hapus(&self.0);
    }
}

fn rekaman(key: &str, alias: Option<&str>, status: u16, latensi: u64, masuk: Option<u64>, keluar: Option<u64>) -> Rekaman {
    Rekaman {
        ts_ms: sekarang_ms(),
        key_id: 1,
        key_name: key.into(),
        alias: alias.map(String::from),
        upstream: alias.map(|_| "utama".to_string()),
        status,
        hasil: klasifikasi(status, None),
        kode_galat: None,
        token_masuk: masuk,
        token_keluar: keluar,
        latensi_ms: latensi,
        percobaan: 1,
        temuan_masuk: 0,
        temuan_keluar: 0,
        jenis_temuan: None,
    }
}

fn semua_waktu() -> (i64, i64) {
    (0, sekarang_ms() + 60_000)
}

#[test]
fn klasifikasi_hasil() {
    assert_eq!(klasifikasi(200, None), "ok");
    assert_eq!(klasifikasi(429, Some("rate_limit_exceeded")), "limit");
    assert_eq!(klasifikasi(400, Some("invalid_json")), "klien");
    assert_eq!(klasifikasi(404, Some("model_not_found")), "klien");
    assert_eq!(klasifikasi(502, Some("upstream_unreachable")), "upstream");
    assert_eq!(klasifikasi(503, Some("upstream_not_configured")), "upstream");
    assert_eq!(klasifikasi(500, Some("internal")), "gateway");
    assert_eq!(klasifikasi(500, None), "upstream", "5xx provider yang diteruskan");
    assert_eq!(klasifikasi(429, None), "upstream", "429 dari provider, bukan dari limiter gateway");
    assert_eq!(klasifikasi(400, None), "klien", "400 dari provider diteruskan = salah request");
}

#[test]
fn baca_usage_dari_respons() {
    let u = baca_usage(br#"{"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}"#).unwrap();
    assert_eq!((u.masuk, u.keluar, u.total), (Some(3), Some(4), Some(7)));
    let u = baca_usage(br#"{"usage":{"total_tokens":9}}"#).unwrap();
    assert_eq!((u.masuk, u.keluar, u.total), (None, None, Some(9)));
    assert!(baca_usage(br#"{"choices":[]}"#).is_none());
    assert!(baca_usage(b"bukan json").is_none());
}

#[test]
fn ringkasan_menjumlah_dan_mengelompokkan() {
    let db = DbTemp::baru("ringkas");
    let s = Statistik::buka(db.path(), 30).unwrap();
    s.catat(rekaman("a", Some("m1"), 200, 100, Some(10), Some(20)));
    s.catat(rekaman("a", Some("m1"), 200, 300, Some(1), Some(2)));
    s.catat(rekaman("a", None, 400, 5, None, None));
    s.catat(rekaman("b", Some("m2"), 502, 50, None, None));
    s.tutup();

    let (d, e) = semua_waktu();
    let semua = s.ringkasan(d, e, Kelompok::Semua).unwrap();
    assert_eq!(semua.len(), 1);
    let t = &semua[0];
    assert_eq!((t.request, t.ok, t.klien, t.upstream, t.limit, t.gateway), (4, 2, 1, 1, 0, 0));
    assert_eq!((t.token_masuk, t.token_keluar), (11, 22));
    assert_eq!(t.latensi_maks_ms, 300);
    assert!((t.latensi_rata_ms - 113.75).abs() < 0.01);

    let per_key = s.ringkasan(d, e, Kelompok::Key).unwrap();
    assert_eq!(
        per_key.iter().map(|b| (b.kelompok.as_str(), b.request)).collect::<Vec<_>>(),
        vec![("a", 3), ("b", 1)],
        "urut dari terbanyak"
    );

    let per_alias = s.ringkasan(d, e, Kelompok::Alias).unwrap();
    let nama: Vec<_> = per_alias.iter().map(|b| b.kelompok.as_str()).collect();
    assert_eq!(nama, vec!["m1", "-", "m2"], "urut jumlah request, lalu nama");

    let per_up = s.ringkasan(d, e, Kelompok::Upstream).unwrap();
    assert!(per_up.iter().any(|b| b.kelompok == "utama" && b.request == 3));

    let per_hari = s.ringkasan(d, e, Kelompok::Hari).unwrap();
    assert_eq!(per_hari.len(), 1);
    assert_eq!(per_hari[0].kelompok.len(), 10, "format YYYY-MM-DD");
}

#[test]
fn filter_waktu_dan_kelompok_tidak_valid() {
    let db = DbTemp::baru("waktu");
    let s = Statistik::buka(db.path(), 30).unwrap();
    s.catat(rekaman("a", Some("m1"), 200, 1, None, None));
    s.tutup();
    let sekarang = sekarang_ms();
    assert_eq!(ringkasan_dari_file(db.path(), sekarang + 10_000, sekarang + 20_000, Kelompok::Semua).unwrap().len(), 0);
    assert!(Kelompok::dari_teks("ngawur").is_err());
    assert!(ringkasan_dari_file("/tidak/ada/stats.db", 0, 1, Kelompok::Semua).is_err());
}

#[test]
fn flush_berkala_tanpa_menunggu_penutupan() {
    let db = DbTemp::baru("berkala");
    let s = Statistik::buka(db.path(), 30).unwrap();
    s.catat(rekaman("a", Some("m1"), 200, 1, None, None));
    std::thread::sleep(Duration::from_millis(1800));
    let (d, e) = semua_waktu();
    assert_eq!(ringkasan_dari_file(db.path(), d, e, Kelompok::Semua).unwrap()[0].request, 1, "tertulis otomatis dalam ~1 detik");
    s.tutup();
}

#[test]
fn banyak_rekaman_semua_tertulis_lintas_batch() {
    let db = DbTemp::baru("banyak");
    let s = Statistik::buka(db.path(), 30).unwrap();
    for _ in 0..650 {
        s.catat(rekaman("a", Some("m1"), 200, 2, Some(1), Some(1)));
    }
    s.tutup();
    let (d, e) = semua_waktu();
    let t = &s.ringkasan(d, e, Kelompok::Semua).unwrap()[0];
    assert_eq!((t.request, t.token_masuk, s.jumlah_dibuang()), (650, 650, 0));
}

#[test]
fn retensi_menghapus_data_lama_saat_dibuka() {
    let db = DbTemp::baru("retensi");
    {
        let s = Statistik::buka(db.path(), 30).unwrap();
        let mut lama = rekaman("a", Some("m1"), 200, 1, None, None);
        lama.ts_ms = sekarang_ms() - 40 * 86_400_000;
        let mut baru_saja = rekaman("a", Some("m1"), 200, 1, None, None);
        baru_saja.ts_ms = sekarang_ms() - 10 * 86_400_000;
        s.catat(lama);
        s.catat(baru_saja);
        s.tutup();
        assert_eq!(s.ringkasan(0, sekarang_ms() + 1000, Kelompok::Semua).unwrap()[0].request, 2);
    }
    let s = Statistik::buka(db.path(), 30).unwrap();
    assert_eq!(s.ringkasan(0, sekarang_ms() + 1000, Kelompok::Semua).unwrap()[0].request, 1, "yang 40 hari terhapus, yang 10 hari tetap");
}

#[test]
fn nonaktif_tidak_mencatat_apa_pun() {
    let s = Statistik::nonaktif();
    s.catat(rekaman("a", None, 200, 1, None, None));
    s.tutup();
    assert!(s.ringkasan(0, i64::MAX, Kelompok::Semua).unwrap().is_empty());
    assert_eq!(s.jumlah_dibuang(), 0);
}

#[test]
fn skema_lebih_baru_ditolak() {
    let db = DbTemp::baru("skema");
    rusqlite::Connection::open(db.path()).unwrap().execute_batch("PRAGMA user_version = 99;").unwrap();
    assert!(Statistik::buka(db.path(), 30).err().unwrap().to_string().contains("lebih baru"));
}

// ---------- end-to-end lewat HTTP ----------

fn upstream_usage() -> Router {
    Router::new().route(
        "/v1/chat/completions",
        post(|| async { Json(json!({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}})) }),
    )
}

fn upstream_500() -> Router {
    Router::new().route("/v1/chat/completions", post(|| async { (StatusCode::INTERNAL_SERVER_ERROR, "rusak") }))
}

fn buat(base: &str, db: &DbTemp) -> (Router, Arc<KeyStore>, Arc<Statistik>) {
    let teks = format!(
        "[resilience]\nretry_backoff_ms = 5\n[[model]]\nalias = \"m1\"\n[[model.upstream]]\nname = \"utama\"\nbase_url = \"{base}\"\nmodel = \"asli\"\n"
    );
    let cfg = Config::from_toml_str(&teks, &|_| None).unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    let stat = Arc::new(Statistik::buka(db.path(), 30).unwrap());
    (app(AppState::new(cfg, keys.clone()).unwrap().dengan_statistik(stat.clone())), keys, stat)
}

async fn kirim(r: &Router, metode: Method, uri: &str, token: Option<&str>, body: &str) -> StatusCode {
    let mut rb = Request::builder().method(metode).uri(uri).header("content-type", "application/json");
    if let Some(t) = token {
        rb = rb.header("authorization", format!("Bearer {t}"));
    }
    let resp = r.clone().oneshot(rb.body(Body::from(body.to_string())).unwrap()).await.unwrap();
    let status = resp.status();
    let _ = resp.into_body().collect().await;
    status
}

type Baris = (String, Option<String>, Option<String>, i64, String, Option<String>, Option<i64>, Option<i64>, i64);

fn baca_baris(db: &DbTemp) -> Vec<Baris> {
    let c = rusqlite::Connection::open(db.path()).unwrap();
    let mut st = c
        .prepare(
            "SELECT key_name, alias, upstream, status, hasil, kode_galat, token_masuk, token_keluar, percobaan FROM requests ORDER BY id",
        )
        .unwrap();
    st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?)))
        .unwrap()
        .map(|x| x.unwrap())
        .collect()
}

#[tokio::test]
async fn setiap_request_chat_tercatat_dengan_rincian_yang_benar() {
    let db = DbTemp::baru("e2e");
    let base = jalankan_upstream(upstream_usage()).await;
    let (r, keys, stat) = buat(&base, &db);
    let (_, ta) = keys.create("tim-a").unwrap();
    let (_, tb) = keys.create("tim-b").unwrap();
    keys.set_limits("tim-b", Some(1), None).unwrap();
    let chat = "/v1/chat/completions";
    let isi_rahasia = r#"{"model":"m1","messages":[{"role":"user","content":"RAHASIA-PROMPT-987654"}]}"#;

    assert_eq!(kirim(&r, Method::POST, chat, Some(&ta), isi_rahasia).await, StatusCode::OK);
    assert_eq!(kirim(&r, Method::POST, chat, Some(&ta), "bukan json").await, StatusCode::BAD_REQUEST);
    assert_eq!(kirim(&r, Method::POST, chat, Some(&ta), r#"{"model":"ngawur-xyz"}"#).await, StatusCode::NOT_FOUND);
    assert_eq!(kirim(&r, Method::POST, chat, Some(&tb), isi_rahasia).await, StatusCode::OK);
    assert_eq!(kirim(&r, Method::POST, chat, Some(&tb), isi_rahasia).await, StatusCode::TOO_MANY_REQUESTS);
    // tidak tercatat: gagal auth dan /v1/models
    assert_eq!(kirim(&r, Method::POST, chat, None, isi_rahasia).await, StatusCode::UNAUTHORIZED);
    assert_eq!(kirim(&r, Method::GET, "/v1/models", Some(&ta), "").await, StatusCode::OK);
    stat.tutup();

    let b = baca_baris(&db);
    assert_eq!(b.len(), 5, "hanya request chat yang lolos auth");
    let ok_a = &b[0];
    assert_eq!(
        (ok_a.0.as_str(), ok_a.1.as_deref(), ok_a.2.as_deref(), ok_a.3, ok_a.4.as_str()),
        ("tim-a", Some("m1"), Some("utama"), 200, "ok")
    );
    assert_eq!((ok_a.6, ok_a.7, ok_a.8), (Some(3), Some(4), 1));
    assert_eq!((b[1].1.as_deref(), b[1].4.as_str(), b[1].5.as_deref()), (None, "klien", Some("invalid_json")));
    assert_eq!((b[2].1.as_deref(), b[2].4.as_str(), b[2].5.as_deref()), (None, "klien", Some("model_not_found")));
    assert_eq!((b[2].1.clone(), b[2].3), (None, 404), "nama model sembarang tidak boleh masuk kolom alias");
    assert_eq!(
        (b[4].0.as_str(), b[4].1.as_deref(), b[4].4.as_str(), b[4].5.as_deref()),
        ("tim-b", Some("m1"), "limit", Some("rate_limit_exceeded"))
    );

    let (d, e) = semua_waktu();
    let per_key = stat.ringkasan(d, e, Kelompok::Key).unwrap();
    let a = per_key.iter().find(|x| x.kelompok == "tim-a").unwrap();
    let bb = per_key.iter().find(|x| x.kelompok == "tim-b").unwrap();
    assert_eq!((a.request, a.ok, a.klien, a.token_masuk, a.token_keluar), (3, 1, 2, 3, 4));
    assert_eq!((bb.request, bb.ok, bb.limit, bb.token_masuk), (2, 1, 1, 3));

    let mentah = std::fs::read(&db.0).unwrap();
    assert!(!mentah.windows(b"RAHASIA-PROMPT-987654".len()).any(|w| w == b"RAHASIA-PROMPT-987654"), "isi prompt tidak boleh tersimpan");
}

#[tokio::test]
async fn kegagalan_upstream_tercatat_sebagai_upstream_dengan_jumlah_percobaan() {
    let db = DbTemp::baru("upstream-gagal");
    let base = jalankan_upstream(upstream_500()).await;
    let (r, keys, stat) = buat(&base, &db);
    let (_, t) = keys.create("tim-a").unwrap();
    assert_eq!(
        kirim(&r, Method::POST, "/v1/chat/completions", Some(&t), r#"{"model":"m1","messages":[]}"#).await,
        StatusCode::INTERNAL_SERVER_ERROR
    );
    stat.tutup();
    let b = baca_baris(&db);
    assert_eq!((b[0].3, b[0].4.as_str(), b[0].8), (500, "upstream", 2));
    assert_eq!(b[0].2, None, "tidak ada upstream yang menjawab dengan sukses");
}

#[test]
fn config_stats_default_dan_validasi() {
    let c = Config::from_toml_str("", &|_| None).unwrap();
    assert_eq!((c.stats_enabled, c.stats_db_path.as_str(), c.stats_retention_days), (true, "nigate-stats.db", 30));
    assert!(Config::from_toml_str("[stats]\nretention_days = 0\n", &|_| None).is_err());
    assert!(Config::from_toml_str("[stats]\ndb_path = \"\"\n", &|_| None).is_err());
    assert!(!Config::from_toml_str("[stats]\nenabled = false\n", &|_| None).unwrap().stats_enabled);
}
