use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use axum::{
    Json, Router,
    body::Body,
    extract::State,
    http::{Method, Request, StatusCode},
    routing::post,
};
use http_body_util::BodyExt;
use nigate::{AppState, app, config::Config, keys::KeyStore, stats::Statistik};
use serde_json::{Value, json};
use tower::ServiceExt;

mod common;
use common::{jalankan_upstream, kode_error};

fn rahasia_palsu(benih: u64) -> String {
    let alfabet: Vec<char> = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789".chars().collect();
    let mut x = benih.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
    let isi: String = (0..36)
        .map(|_| {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            alfabet[((x >> 33) as usize) % alfabet.len()]
        })
        .collect();
    format!("{}_{}", "ghp", isi)
}

struct DbTemp(PathBuf);

impl DbTemp {
    fn baru(nama: &str) -> Self {
        let p = std::env::temp_dir().join(format!("nigate-guard-{nama}-{}.db", std::process::id()));
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

#[derive(Clone)]
struct Up {
    diterima: Arc<Mutex<Vec<Value>>>,
    balasan: Arc<Value>,
}

fn upstream(balasan: Value) -> (Router, Arc<Mutex<Vec<Value>>>) {
    let diterima: Arc<Mutex<Vec<Value>>> = Arc::default();
    let r = Router::new()
        .route(
            "/v1/chat/completions",
            post(|State(u): State<Up>, Json(b): Json<Value>| async move {
                u.diterima.lock().unwrap().push(b);
                Json((*u.balasan).clone())
            }),
        )
        .with_state(Up { diterima: diterima.clone(), balasan: Arc::new(balasan) });
    (r, diterima)
}

fn balasan_teks(teks: &str) -> Value {
    json!({
        "id": "x",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": teks}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    })
}

fn buat(base: &str, guardrail_toml: &str, db: &DbTemp) -> (Router, String, Arc<KeyStore>, Arc<Statistik>) {
    let teks = format!(
        "{guardrail_toml}\n[[model]]\nalias = \"m1\"\n[[model.upstream]]\nname = \"utama\"\nbase_url = \"{base}\"\nmodel = \"asli\"\n"
    );
    let cfg = Config::from_toml_str(&teks, &|_| None).unwrap();
    let keys = Arc::new(KeyStore::open_memory().unwrap());
    let (_, token) = keys.create("tim-a").unwrap();
    let stat = Arc::new(Statistik::buka(db.path(), 30).unwrap());
    let r = app(AppState::new(cfg, keys.clone()).unwrap().dengan_statistik(stat.clone()));
    (r, token, keys, stat)
}

async fn chat(r: &Router, token: &str, body: &Value) -> (StatusCode, String) {
    let req = Request::builder()
        .method(Method::POST)
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .header("authorization", format!("Bearer {token}"))
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = r.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).to_string())
}

type Baris = (String, Option<String>, i64, i64, Option<String>);

/// (hasil, kode_galat, temuan_masuk, temuan_keluar, jenis_temuan)
fn baca(db: &DbTemp) -> Vec<Baris> {
    let c = rusqlite::Connection::open(db.path()).unwrap();
    let mut st = c.prepare("SELECT hasil, kode_galat, temuan_masuk, temuan_keluar, jenis_temuan FROM requests ORDER BY id").unwrap();
    st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))).unwrap().map(|x| x.unwrap()).collect()
}

fn permintaan_dengan_rahasia(rahasia: &str) -> Value {
    json!({
        "model": "m1",
        "messages": [
            {"role": "user", "content": format!("pakai kunci {rahasia} ya")},
            {"role": "assistant", "content": null, "tool_calls": [
                {"id": "call_Ab12Cd", "type": "function", "function": {"name": "f", "arguments": "{\"x\":1}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_Ab12Cd", "content": format!("hasil tool memuat {rahasia}")}
        ]
    })
}

#[tokio::test]
async fn secret_pada_request_tidak_pernah_sampai_ke_upstream_dan_tercatat() {
    let db = DbTemp::baru("req-redact");
    let (up, diterima) = upstream(balasan_teks("oke"));
    let base = jalankan_upstream(up).await;
    let (r, token, _, stat) = buat(&base, "", &db);
    let rahasia = rahasia_palsu(1);

    let (status, _) = chat(&r, &token, &permintaan_dengan_rahasia(&rahasia)).await;
    assert_eq!(status, StatusCode::OK);
    stat.tutup();

    let d = diterima.lock().unwrap();
    assert_eq!(d.len(), 1);
    let sampai = d[0].to_string();
    assert!(!sampai.contains(&rahasia), "SECRET BOCOR ke upstream: {sampai}");
    assert_eq!(sampai.matches("[REDACTED:github_token]").count(), 2, "pesan user dan pesan tool");
    assert_eq!(d[0]["model"], "asli");
    assert_eq!(d[0]["messages"][2]["tool_call_id"], "call_Ab12Cd", "id tool call tetap utuh");
    assert_eq!(d[0]["messages"][1]["tool_calls"][0]["id"], "call_Ab12Cd");

    let b = baca(&db);
    assert_eq!(b[0], ("ok".to_string(), None, 2, 0, Some("github_token".to_string())));
    let mentah = std::fs::read(&db.0).unwrap();
    assert!(!mentah.windows(rahasia.len()).any(|w| w == rahasia.as_bytes()), "secret tidak boleh masuk database statistik");
}

#[tokio::test]
async fn secret_pada_respons_diredaksi_sebelum_sampai_ke_klien() {
    let db = DbTemp::baru("resp-redact");
    let rahasia = rahasia_palsu(2);
    let (up, _) = upstream(balasan_teks(&format!("kunci Anda: {rahasia}")));
    let base = jalankan_upstream(up).await;
    let (r, token, _, stat) = buat(&base, "", &db);

    let (status, body) = chat(&r, &token, &json!({"model": "m1", "messages": [{"role": "user", "content": "siapa kunci saya?"}]})).await;
    assert_eq!(status, StatusCode::OK);
    assert!(!body.contains(&rahasia), "SECRET BOCOR ke klien: {body}");
    let v: Value = serde_json::from_str(&body).expect("respons tetap JSON valid");
    assert_eq!(v["choices"][0]["message"]["content"], "kunci Anda: [REDACTED:github_token]");
    assert_eq!(v["usage"]["total_tokens"], 7, "field lain tidak berubah");
    stat.tutup();
    assert_eq!(baca(&db)[0], ("ok".to_string(), None, 0, 1, Some("github_token".to_string())));
}

#[tokio::test]
async fn mode_block_menolak_request_tanpa_menghubungi_upstream_dan_tanpa_memakai_jatah() {
    let db = DbTemp::baru("block-req");
    let (up, diterima) = upstream(balasan_teks("oke"));
    let base = jalankan_upstream(up).await;
    let (r, token, keys, stat) = buat(&base, "[guardrail]\nmode = \"block\"\n", &db);
    keys.set_limits("tim-a", Some(1), None).unwrap();
    let rahasia = rahasia_palsu(3);

    let (status, body) = chat(&r, &token, &permintaan_dengan_rahasia(&rahasia)).await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(kode_error(&body), "guardrail_blocked");
    assert!(body.contains("github_token") && !body.contains(&rahasia), "pesan menyebut jenis, bukan isinya");
    assert_eq!(diterima.lock().unwrap().len(), 0, "upstream tidak boleh dihubungi");

    let (status, _) = chat(&r, &token, &json!({"model": "m1", "messages": [{"role": "user", "content": "halo"}]})).await;
    assert_eq!(status, StatusCode::OK, "jatah RPM 1 masih utuh setelah request yang diblok");
    stat.tutup();
    let b = baca(&db);
    assert_eq!(b[0], ("guardrail".to_string(), Some("guardrail_blocked".to_string()), 2, 0, Some("github_token".to_string())));
    assert_eq!(b[1].0, "ok");
}

#[tokio::test]
async fn respons_yang_diblok_tidak_dibocorkan_ke_klien() {
    let db = DbTemp::baru("block-resp");
    let rahasia = rahasia_palsu(4);
    let (up, _) = upstream(balasan_teks(&format!("nih {rahasia}")));
    let base = jalankan_upstream(up).await;
    let (r, token, _, stat) = buat(&base, "[guardrail.aksi]\ngithub_token = \"block\"\n", &db);

    let (status, body) = chat(&r, &token, &json!({"model": "m1", "messages": [{"role": "user", "content": "x"}]})).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert_eq!(kode_error(&body), "guardrail_blocked");
    assert!(!body.contains(&rahasia));
    stat.tutup();
    assert_eq!(baca(&db)[0], ("guardrail".to_string(), Some("guardrail_blocked".to_string()), 0, 1, Some("github_token".to_string())));
}

#[tokio::test]
async fn log_only_meneruskan_apa_adanya_tetapi_tetap_mencatat_temuan() {
    let db = DbTemp::baru("logonly");
    let (up, diterima) = upstream(balasan_teks("oke"));
    let base = jalankan_upstream(up).await;
    let (r, token, _, stat) = buat(&base, "[guardrail]\nmode = \"log_only\"\n", &db);
    let rahasia = rahasia_palsu(5);

    let (status, _) = chat(&r, &token, &permintaan_dengan_rahasia(&rahasia)).await;
    assert_eq!(status, StatusCode::OK);
    assert!(diterima.lock().unwrap()[0].to_string().contains(&rahasia), "log_only tidak mengubah isi");
    stat.tutup();
    assert_eq!(baca(&db)[0].2, 2);
}

#[tokio::test]
async fn guardrail_dimatikan_tidak_menyentuh_apa_pun() {
    let db = DbTemp::baru("mati");
    let (up, diterima) = upstream(balasan_teks("oke"));
    let base = jalankan_upstream(up).await;
    let (r, token, _, stat) = buat(&base, "[guardrail]\nenabled = false\n", &db);
    let rahasia = rahasia_palsu(6);
    let (status, _) = chat(&r, &token, &permintaan_dengan_rahasia(&rahasia)).await;
    assert_eq!(status, StatusCode::OK);
    assert!(diterima.lock().unwrap()[0].to_string().contains(&rahasia));
    stat.tutup();
    assert_eq!(baca(&db)[0].2, 0);
}

#[tokio::test]
async fn request_bersih_diteruskan_persis_sama_kecuali_nama_model() {
    let db = DbTemp::baru("bersih");
    let (up, diterima) = upstream(balasan_teks("halo juga"));
    let base = jalankan_upstream(up).await;
    let (r, token, _, stat) = buat(&base, "", &db);
    let asli = json!({
        "model": "m1", "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "Kamu asisten analitik penjualan."},
            {"role": "user", "content": [{"type": "text", "text": "Ringkas penjualan Agustus."}]}
        ],
        "tools": [{"type": "function", "function": {"name": "cari", "parameters": {"type": "object"}}}]
    });
    let (status, body) = chat(&r, &token, &asli).await;
    assert_eq!(status, StatusCode::OK);
    let mut harapan = asli.clone();
    harapan["model"] = json!("asli");
    assert_eq!(diterima.lock().unwrap()[0], harapan);
    assert_eq!(serde_json::from_str::<Value>(&body).unwrap(), balasan_teks("halo juga"));
    stat.tutup();
    assert_eq!(baca(&db)[0], ("ok".to_string(), None, 0, 0, None));
}
