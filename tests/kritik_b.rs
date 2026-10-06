//! Regresi dari kritik independen (Batch B): kebenaran di tepi (budget waktu, pembatalan klien, timeout baca body, pekerja statistik).
//! Setiap tes ditulis SEBELUM perbaikannya dan harus gagal pada kode lama.

use std::path::PathBuf;
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
use std::time::{Duration, Instant};

use axum::{
    Router,
    body::Body,
    extract::State,
    http::{Method, Request, StatusCode},
    routing::post,
};
use http_body_util::BodyExt;
use nigate::{AppState, app, config::Config, keys::KeyStore, stats::Statistik};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tower::ServiceExt;

mod common;
use common::{jalankan_upstream, kode_error};

struct DbTemp(PathBuf);

impl DbTemp {
    fn baru(nama: &str) -> Self {
        let p = std::env::temp_dir().join(format!("nigate-kritikb-{nama}-{}-{:?}.db", std::process::id(), std::thread::current().id()));
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

impl Drop for DbTemp {
    fn drop(&mut self) {
        Self::hapus(&self.0);
    }
}

/// Upstream yang menunda jawaban `tunda` pada panggilan pertama saja.
#[derive(Clone)]
struct Lambat {
    tunda: Duration,
    hanya_pertama: bool,
    hits: Arc<AtomicUsize>,
}

async fn upstream_lambat(tunda: Duration, hanya_pertama: bool) -> (String, Arc<AtomicUsize>) {
    let hits: Arc<AtomicUsize> = Arc::default();
    let r = Router::new()
        .route(
            "/v1/chat/completions",
            post(|State(l): State<Lambat>| async move {
                let n = l.hits.fetch_add(1, Ordering::SeqCst);
                if !l.hanya_pertama || n == 0 {
                    tokio::time::sleep(l.tunda).await;
                }
                axum::Json(json!({"choices": [], "usage": {"total_tokens": 5}}))
            }),
        )
        .with_state(Lambat { tunda, hanya_pertama, hits: hits.clone() });
    (jalankan_upstream(r).await, hits)
}

fn req_chat(body: String, token: Option<&str>) -> Request<Body> {
    let mut rb = Request::builder().method(Method::POST).uri("/v1/chat/completions").header("content-type", "application/json");
    if let Some(t) = token {
        rb = rb.header("authorization", format!("Bearer {t}"));
    }
    rb.body(Body::from(body)).unwrap()
}

// ---------- K-1: budget total habis tetap mencatat kegagalan upstream ----------

#[tokio::test]
async fn budget_total_habis_di_tengah_retry_tetap_memasukkan_upstream_ke_cooldown() {
    let (base, _) = upstream_lambat(Duration::from_secs(3), false).await;
    let t = format!(
        "[auth]\nrequired = false\n[resilience]\nmax_retries = 3\nretry_backoff_ms = 5\ncooldown_secs = 60\ntotal_timeout_secs = 1\n\
         [[model]]\nalias = \"m1\"\n[[model.upstream]]\nname = \"macet\"\nbase_url = \"{base}\"\nmodel = \"x\"\ntimeout_secs = 5\n"
    );
    let cfg = Config::from_toml_str(&t, &|_| None).unwrap();
    let st = AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap())).unwrap();
    let r = app(st.clone());

    let resp = r.oneshot(req_chat(r#"{"model":"m1","messages":[]}"#.into(), None)).await.unwrap();
    assert_eq!(resp.status(), StatusCode::GATEWAY_TIMEOUT);
    let status = st.kesehatan.status(&format!("{base}|x"), Instant::now());
    assert_eq!(status.gagal_beruntun, 1, "upstream yang macet harus tercatat gagal walau budget habis di tengah retry");
    assert!(status.sisa_cooldown.is_some(), "dan masuk cooldown supaya tidak terus membakar seluruh budget");
}

// ---------- K-3: body yang macet = timeout (504), bukan 'terputus' (502) ----------

#[tokio::test]
async fn body_respons_yang_macet_dijawab_504_timeout() {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        loop {
            let (mut s, _) = l.accept().await.unwrap();
            tokio::spawn(async move {
                let mut buf = [0u8; 8192];
                let _ = s.read(&mut buf).await;
                let _ = s.write_all(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 1000\r\n\r\n{\"a\":").await;
                tokio::time::sleep(Duration::from_secs(10)).await;
            });
        }
    });
    let t = format!(
        "[auth]\nrequired = false\n[resilience]\nmax_retries = 0\n[[model]]\nalias = \"m1\"\n[[model.upstream]]\nname = \"macet\"\n\
         base_url = \"http://{addr}/v1\"\nmodel = \"x\"\ntimeout_secs = 1\n"
    );
    let cfg = Config::from_toml_str(&t, &|_| None).unwrap();
    let r = app(AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap())).unwrap());
    let resp = r.oneshot(req_chat(r#"{"model":"m1","messages":[]}"#.into(), None)).await.unwrap();
    let status = resp.status();
    let b = resp.into_body().collect().await.unwrap().to_bytes();
    assert_eq!((status, kode_error(&String::from_utf8_lossy(&b)).as_str()), (StatusCode::GATEWAY_TIMEOUT, "upstream_timeout"));
}

// ---------- K-2: request yang dibatalkan klien tetap tercatat dan jatah TPM dikembalikan ----------

#[tokio::test]
async fn request_dibatalkan_klien_tercatat_499_dan_jatah_tpm_dikembalikan() {
    let (base, hits) = upstream_lambat(Duration::from_secs(3), true).await;
    let db = DbTemp::baru("batal");
    let t =
        format!("[[model]]\nalias = \"m1\"\n[[model.upstream]]\nname = \"u\"\nbase_url = \"{base}\"\nmodel = \"x\"\ntimeout_secs = 10\n");
    let cfg = Config::from_toml_str(&t, &|_| None).unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    let (_, token) = keys.create("klien").unwrap();
    keys.set_limits("klien", None, Some(1000)).unwrap();
    let stat = Arc::new(Statistik::buka(db.path(), 30).unwrap());
    let r = app(AppState::new(cfg, keys).unwrap().dengan_statistik(stat.clone()));

    // ~3 KB -> estimasi ~750 token dari bucket TPM 1000
    let besar = json!({"model": "m1", "messages": [{"role": "user", "content": "x".repeat(2900)}]}).to_string();
    let batal = tokio::time::timeout(Duration::from_millis(300), r.clone().oneshot(req_chat(besar.clone(), Some(&token)))).await;
    assert!(batal.is_err(), "klien menyerah lebih dulu (upstream butuh 3 detik)");
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(hits.load(Ordering::SeqCst), 1);

    // jatah TPM harus sudah dikembalikan: request berikutnya yang sama besar harus lolos (upstream cepat sekarang)
    let kedua = r.clone().oneshot(req_chat(besar, Some(&token))).await.unwrap();
    assert_eq!(kedua.status(), StatusCode::OK, "estimasi token dari request yang dibatalkan harus dikembalikan");
    let _ = kedua.into_body().collect().await;

    stat.tutup();
    let c = rusqlite::Connection::open(db.path()).unwrap();
    let status: Vec<i64> =
        c.prepare("SELECT status FROM requests ORDER BY id").unwrap().query_map([], |x| x.get(0)).unwrap().map(|x| x.unwrap()).collect();
    assert_eq!(status.len(), 2, "request yang dibatalkan juga harus punya baris statistik: {status:?}");
    assert!(status.contains(&499), "dicatat sebagai 499 (klien menutup koneksi): {status:?}");
    let (hasil, kode): (String, Option<String>) =
        c.query_row("SELECT hasil, kode_galat FROM requests WHERE status = 499", [], |x| Ok((x.get(0)?, x.get(1)?))).unwrap();
    assert_eq!((hasil.as_str(), kode.as_deref()), ("klien", Some("client_cancelled")));
}

// ---------- K-4: pekerja statistik tidak berputar sia-sia saat idle ----------

#[test]
fn pekerja_statistik_tidak_berputar_terus_saat_idle() {
    let db = DbTemp::baru("idle");
    let s = Statistik::buka(db.path(), 30).unwrap();
    std::thread::sleep(Duration::from_millis(1500));
    let siklus = s.jumlah_siklus_pekerja();
    assert!(siklus < 10, "pekerja berputar {siklus}x dalam 1,5 detik idle (harus menunggu tanpa polling 10 ms)");
}

#[test]
fn rekaman_setelah_jeda_idle_tetap_ditulis_berkala() {
    let db = DbTemp::baru("setelah-idle");
    let s = Statistik::buka(db.path(), 30).unwrap();
    std::thread::sleep(Duration::from_millis(1200));
    s.catat(nigate::stats::Rekaman {
        ts_ms: nigate::stats::sekarang_ms(),
        key_id: 1,
        key_name: "a".into(),
        alias: None,
        upstream: None,
        end_user: None,
        status: 200,
        hasil: "ok",
        kode_galat: None,
        token_masuk: None,
        token_keluar: None,
        latensi_ms: 1,
        percobaan: 1,
        temuan_masuk: 0,
        temuan_keluar: 0,
        jenis_temuan: None,
    });
    std::thread::sleep(Duration::from_millis(1800));
    let n: i64 = rusqlite::Connection::open(db.path()).unwrap().query_row("SELECT COUNT(*) FROM requests", [], |x| x.get(0)).unwrap();
    assert_eq!(n, 1, "tertulis otomatis dalam ~1 detik walau pekerja sempat idle lama");
    s.tutup();
}
