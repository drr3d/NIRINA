//! API admin: listener terpisah dari jalur chat, dilindungi token admin, untuk dipakai UI (Streamlit) dan operator.
//! Semua respons JSON; galat memakai format yang sama dengan jalur chat. Tidak pernah mengembalikan isi prompt/jawaban,
//! key provider, hash key, maupun token admin.

use std::{collections::HashMap, sync::Arc};

use axum::{
    Json, Router,
    body::Bytes,
    extract::{DefaultBodyLimit, Path, RawQuery, Request, State},
    http::{HeaderValue, StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, patch, post, put},
};
use serde::{Deserialize, Deserializer, de::DeserializeOwned};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::{
    config::Config,
    error::ApiError,
    keys::{Client, KeyInfo, Metadata, NamaDipakai, validasi_metadata, validasi_nama, validasi_user},
    proxy::AppState,
    stats::{Kelompok, sekarang_ms},
    util::PembatasLog,
};

const MAKS_JAM: u64 = 24 * 365;

/// Percobaan token salah bisa dipicu dari luar: log dibatasi satu baris per 10 detik.
static LOG_AUTH_GAGAL: PembatasLog = PembatasLog::baru(10_000);

pub fn admin_app(state: AppState) -> Router {
    Router::new()
        .route("/admin/health", get(health))
        .route("/admin/config", get(config_efektif))
        .route("/admin/reload", post(muat_ulang))
        .route("/admin/keys", get(daftar_key).post(buat_key))
        .route("/admin/keys/{nama}", patch(ubah_key).delete(hapus_key))
        .route("/admin/keys/{nama}/users", get(daftar_client))
        .route("/admin/keys/{nama}/users/{user}", put(simpan_client).delete(hapus_client))
        .route("/admin/upstreams", get(status_upstream))
        .route("/admin/stats", get(statistik))
        .route("/admin/guardrail/events", get(kejadian_guardrail))
        .layer(DefaultBodyLimit::max(64 * 1024))
        .layer(middleware::from_fn_with_state(state.clone(), auth_admin))
        .with_state(state)
}

// ---------- auth ----------

/// Perbandingan waktu-konstan lewat hash, supaya panjang/isi token tidak bocor lewat waktu respons.
fn token_sama(a: &str, b: &str) -> bool {
    let (ha, hb) = (Sha256::digest(a.as_bytes()), Sha256::digest(b.as_bytes()));
    ha.iter().zip(hb.iter()).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

async fn auth_admin(State(s): State<AppState>, req: Request, next: Next) -> Response {
    let rt = s.runtime();
    let diberikan = req
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.split_once(' '))
        .filter(|(skema, _)| skema.eq_ignore_ascii_case("bearer"))
        .map(|(_, t)| t.trim().to_string());

    let sah = match (&rt.config.admin_token, &diberikan) {
        (Some(harapan), Some(t)) => token_sama(harapan, t),
        _ => false,
    };
    let mut resp = if sah {
        next.run(req).await
    } else {
        if LOG_AUTH_GAGAL.boleh() {
            tracing::warn!(jalur = %req.uri().path(), "admin: token tidak valid atau tidak ada (log dibatasi 1 baris/10 dtk)");
        }
        let mut r = ApiError::new(StatusCode::UNAUTHORIZED, "invalid_request_error", "invalid_admin_token", "Token admin tidak valid.")
            .into_response();
        r.headers_mut().insert(header::WWW_AUTHENTICATE, HeaderValue::from_static("Bearer"));
        r
    };
    resp.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    resp
}

// ---------- util ----------

type Hasil = Result<Json<Value>, ApiError>;

fn galat(status: StatusCode, code: &'static str, pesan: impl Into<String>) -> ApiError {
    let kind = if status.is_server_error() { "api_error" } else { "invalid_request_error" };
    ApiError::new(status, kind, code, pesan)
}

fn galat_internal(e: impl std::fmt::Display) -> ApiError {
    tracing::error!("admin: galat internal: {e}");
    galat(StatusCode::INTERNAL_SERVER_ERROR, "internal", "Galat internal.")
}

fn baca_body<T: DeserializeOwned>(b: &Bytes) -> Result<T, ApiError> {
    serde_json::from_slice(b).map_err(|e| galat(StatusCode::BAD_REQUEST, "invalid_body", format!("Body JSON tidak valid: {e}")))
}

fn query(raw: Option<String>) -> HashMap<String, String> {
    raw.unwrap_or_default().split('&').filter_map(|p| p.split_once('=')).map(|(k, v)| (k.to_string(), v.to_string())).collect()
}

fn angka(q: &HashMap<String, String>, kunci: &'static str, bawaan: u64, min: u64, maks: u64) -> Result<u64, ApiError> {
    match q.get(kunci) {
        None => Ok(bawaan),
        Some(v) => v
            .parse::<u64>()
            .ok()
            .filter(|n| (min..=maks).contains(n))
            .ok_or_else(|| galat(StatusCode::BAD_REQUEST, "invalid_query", format!("Parameter '{kunci}' harus angka {min}..={maks}."))),
    }
}

/// Membuang bagian user:password@ dari URL sebelum ditampilkan.
fn url_aman(url: &str) -> String {
    match url.split_once("://") {
        Some((skema, sisa)) => {
            let (host, jalur) = sisa.split_once('/').map_or((sisa, ""), |(h, j)| (h, j));
            let host = host.rsplit_once('@').map_or(host, |(_, h)| h);
            if jalur.is_empty() { format!("{skema}://{host}") } else { format!("{skema}://{host}/{jalur}") }
        }
        None => url.to_string(),
    }
}

fn key_json(k: &KeyInfo, cfg: &Config) -> Value {
    json!({
        "id": k.id, "name": k.name, "prefix": k.prefix, "active": k.active, "created_at": k.created_at,
        "rpm": k.rpm, "tpm": k.tpm, "metadata": k.metadata,
        "user_rpm": k.user_rpm, "user_tpm": k.user_tpm, "user_required": k.user_required,
        "rpm_efektif": k.rpm.or(cfg.default_rpm), "tpm_efektif": k.tpm.or(cfg.default_tpm),
    })
}

// ---------- endpoint ----------

async fn health(State(s): State<AppState>) -> Json<Value> {
    let rt = s.runtime();
    Json(json!({
        "status": "ok",
        "versi": env!("CARGO_PKG_VERSION"),
        "uptime_detik": s.mulai.elapsed().as_secs(),
        "auth_required": rt.config.auth_required,
        "jumlah_model": rt.config.models.len(),
        "stats_aktif": rt.config.stats_enabled,
        "statistik_dibuang": s.statistik.jumlah_dibuang(),
        "guardrail_aktif": rt.config.guardrail.aktif,
        "reload_tersedia": s.config_path.is_some(),
    }))
}

/// Operasi SQLite (disk) dijalankan di thread pemblokir supaya tidak menahan worker async.
async fn blok<T, F>(kerja: F) -> Result<T, ApiError>
where
    T: Send + 'static,
    F: FnOnce() -> anyhow::Result<T> + Send + 'static,
{
    tokio::task::spawn_blocking(kerja).await.map_err(galat_internal)?.map_err(galat_internal)
}

async fn daftar_key(State(s): State<AppState>) -> Hasil {
    let rt = s.runtime();
    let keys = Arc::clone(&s.keys);
    let daftar = blok(move || keys.list()).await?;
    Ok(Json(json!({ "keys": daftar.iter().map(|k| key_json(k, &rt.config)).collect::<Vec<_>>() })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BuatKey {
    name: String,
    rpm: Option<u64>,
    tpm: Option<u64>,
    metadata: Option<Value>,
    /// Batas bawaan per client (field `user`) dan kewajiban mengirim `user`.
    user_rpm: Option<u64>,
    user_tpm: Option<u64>,
    user_required: Option<bool>,
}

/// Metadata dari body JSON: objek berisi teks. null/tidak ada = kosong. Diterima sebagai `Value` (bukan langsung peta teks)
/// supaya galatnya spesifik (`invalid_metadata`), bukan galat parse body umum.
fn metadata_dari(v: Option<Value>) -> Result<Metadata, ApiError> {
    let tolak = |p: String| galat(StatusCode::BAD_REQUEST, "invalid_metadata", p);
    let m: Metadata = match v {
        None | Some(Value::Null) => Metadata::new(),
        Some(Value::Object(o)) => o
            .into_iter()
            .map(|(k, v)| match v {
                Value::String(t) => Ok((k, t)),
                _ => Err(tolak(format!("Nilai metadata '{k}' harus teks."))),
            })
            .collect::<Result<_, _>>()?,
        Some(_) => return Err(tolak("Metadata harus objek JSON berisi teks, mis. {\"client_id\": \"mall-a\"}.".into())),
    };
    validasi_metadata(&m).map_err(|e| tolak(format!("Metadata tidak valid: {e}.")))?;
    Ok(m)
}

fn validasi_batas(rpm: Option<u64>, tpm: Option<u64>) -> Result<(), ApiError> {
    cek_batas(&[("rpm", rpm), ("tpm", tpm)])
}

fn cek_batas(daftar: &[(&str, Option<u64>)]) -> Result<(), ApiError> {
    for &(label, v) in daftar {
        if v.is_some_and(|x| x == 0 || x > i64::MAX as u64) {
            return Err(galat(StatusCode::BAD_REQUEST, "invalid_limit", format!("{label} harus >= 1 (atau null untuk tanpa batas).")));
        }
    }
    Ok(())
}

async fn buat_key(State(s): State<AppState>, body: Bytes) -> Result<(StatusCode, Json<Value>), ApiError> {
    let b: BuatKey = baca_body(&body)?;
    validasi_nama(&b.name).map_err(|e| galat(StatusCode::BAD_REQUEST, "invalid_name", e.to_string()))?;
    validasi_batas(b.rpm, b.tpm)?;
    cek_batas(&[("user_rpm", b.user_rpm), ("user_tpm", b.user_tpm)])?;
    let metadata = metadata_dari(b.metadata)?;
    let aturan_client = (b.user_rpm, b.user_tpm, b.user_required.unwrap_or(false));

    let (keys, nama, rpm, tpm) = (Arc::clone(&s.keys), b.name.clone(), b.rpm, b.tpm);
    let dibuat = blok(move || match keys.create(&nama) {
        Ok((info, token)) => {
            if rpm.is_some() || tpm.is_some() {
                keys.set_limits(&nama, rpm, tpm)?;
            }
            if !metadata.is_empty() {
                keys.set_metadata(&nama, &metadata)?;
            }
            if aturan_client != (None, None, false) {
                keys.set_aturan_client(&nama, aturan_client.0, aturan_client.1, aturan_client.2)?;
            }
            Ok(Some((keys.get(&nama)?.unwrap_or(info), token)))
        }
        Err(e) if e.downcast_ref::<NamaDipakai>().is_some() => Ok(None),
        Err(e) => Err(e),
    })
    .await?;

    let Some((info, token)) = dibuat else {
        return Err(galat(StatusCode::CONFLICT, "name_taken", format!("Nama key '{}' sudah dipakai.", b.name)));
    };
    // Key asli hanya dikembalikan di sini, sekali ini saja.
    Ok((StatusCode::CREATED, Json(json!({ "key": token, "info": key_json(&info, &s.runtime().config) }))))
}

/// Membedakan field yang tidak dikirim (biarkan) dari yang dikirim null (hapus batas/metadata).
fn ganda<'de, D: Deserializer<'de>, T: Deserialize<'de>>(d: D) -> Result<Option<Option<T>>, D::Error> {
    Ok(Some(Option::<T>::deserialize(d)?))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UbahKey {
    active: Option<bool>,
    #[serde(default, deserialize_with = "ganda")]
    rpm: Option<Option<u64>>,
    #[serde(default, deserialize_with = "ganda")]
    tpm: Option<Option<u64>>,
    /// Mengganti seluruh metadata; null = kosongkan.
    #[serde(default, deserialize_with = "ganda")]
    metadata: Option<Option<Value>>,
    #[serde(default, deserialize_with = "ganda")]
    user_rpm: Option<Option<u64>>,
    #[serde(default, deserialize_with = "ganda")]
    user_tpm: Option<Option<u64>>,
    user_required: Option<bool>,
}

async fn ubah_key(State(s): State<AppState>, Path(nama): Path<String>, body: Bytes) -> Hasil {
    let p: UbahKey = baca_body(&body)?;
    let ubah_client = p.user_rpm.is_some() || p.user_tpm.is_some() || p.user_required.is_some();
    if p.active.is_none() && p.rpm.is_none() && p.tpm.is_none() && p.metadata.is_none() && !ubah_client {
        return Err(galat(
            StatusCode::BAD_REQUEST,
            "no_changes",
            "Tidak ada perubahan: kirim active, rpm, tpm, metadata, user_rpm, user_tpm, atau user_required.",
        ));
    }
    // Hanya nilai yang dikirim yang perlu divalidasi; nilai lama di database sudah valid.
    validasi_batas(p.rpm.flatten(), p.tpm.flatten())?;
    cek_batas(&[("user_rpm", p.user_rpm.flatten()), ("user_tpm", p.user_tpm.flatten())])?;
    let metadata = p.metadata.map(metadata_dari).transpose()?;

    let (keys, nama2) = (Arc::clone(&s.keys), nama.clone());
    let baru = blok(move || {
        let Some(lama) = keys.get(&nama2)? else { return Ok(None) };
        if p.rpm.is_some() || p.tpm.is_some() {
            keys.set_limits(&nama2, p.rpm.unwrap_or(lama.rpm), p.tpm.unwrap_or(lama.tpm))?;
        }
        if let Some(m) = &metadata {
            keys.set_metadata(&nama2, m)?;
        }
        if ubah_client {
            keys.set_aturan_client(
                &nama2,
                p.user_rpm.unwrap_or(lama.user_rpm),
                p.user_tpm.unwrap_or(lama.user_tpm),
                p.user_required.unwrap_or(lama.user_required),
            )?;
        }
        if let Some(a) = p.active {
            keys.set_active(&nama2, a)?;
        }
        keys.get(&nama2)
    })
    .await?
    .ok_or_else(|| galat(StatusCode::NOT_FOUND, "key_not_found", format!("Key '{nama}' tidak ditemukan.")))?;
    Ok(Json(json!({ "info": key_json(&baru, &s.runtime().config) })))
}

async fn hapus_key(State(s): State<AppState>, Path(nama): Path<String>) -> Hasil {
    let (keys, nama2) = (Arc::clone(&s.keys), nama.clone());
    if !blok(move || keys.remove(&nama2)).await? {
        return Err(galat(StatusCode::NOT_FOUND, "key_not_found", format!("Key '{nama}' tidak ditemukan.")));
    }
    Ok(Json(json!({ "dihapus": nama })))
}

// ---------- client (label `user`) di bawah key ----------

fn client_json(c: &Client, k: &KeyInfo) -> Value {
    json!({
        "user": c.user, "rpm": c.rpm, "tpm": c.tpm, "active": c.active, "created_at": c.created_at,
        "rpm_efektif": c.rpm.or(k.user_rpm), "tpm_efektif": c.tpm.or(k.user_tpm),
    })
}

fn key_tidak_ada(nama: &str) -> ApiError {
    galat(StatusCode::NOT_FOUND, "key_not_found", format!("Key '{nama}' tidak ditemukan."))
}

async fn daftar_client(State(s): State<AppState>, Path(nama): Path<String>) -> Hasil {
    let (keys, nama2) = (Arc::clone(&s.keys), nama.clone());
    let hasil = blok(move || Ok(keys.get(&nama2)?.zip(keys.list_clients(&nama2)?))).await?;
    let (k, daftar) = hasil.ok_or_else(|| key_tidak_ada(&nama))?;
    Ok(Json(json!({
        "key": k.name,
        "user_rpm": k.user_rpm, "user_tpm": k.user_tpm, "user_required": k.user_required,
        "users": daftar.iter().map(|c| client_json(c, &k)).collect::<Vec<_>>(),
    })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SimpanClient {
    /// null/tidak ada = ikut batas bawaan per client dari key.
    rpm: Option<u64>,
    tpm: Option<u64>,
    /// false = blokir client. Bawaan true.
    active: Option<bool>,
}

/// Membuat atau mengganti seluruh pengaturan khusus satu client.
async fn simpan_client(State(s): State<AppState>, Path((nama, user)): Path<(String, String)>, body: Bytes) -> Hasil {
    let b: SimpanClient = if body.is_empty() { SimpanClient { rpm: None, tpm: None, active: None } } else { baca_body(&body)? };
    validasi_user(&user).map_err(|e| galat(StatusCode::BAD_REQUEST, "invalid_user", format!("{e}.")))?;
    validasi_batas(b.rpm, b.tpm)?;
    let (keys, nama2) = (Arc::clone(&s.keys), nama.clone());
    let hasil = blok(move || {
        let Some(c) = keys.set_client(&nama2, &user, b.rpm, b.tpm, b.active.unwrap_or(true))? else { return Ok(None) };
        Ok(keys.get(&nama2)?.map(|k| (c, k)))
    })
    .await?;
    let (c, k) = hasil.ok_or_else(|| key_tidak_ada(&nama))?;
    Ok(Json(json!({ "user": client_json(&c, &k) })))
}

async fn hapus_client(State(s): State<AppState>, Path((nama, user)): Path<(String, String)>) -> Hasil {
    let (keys, nama2, user2) = (Arc::clone(&s.keys), nama.clone(), user.clone());
    match blok(move || keys.remove_client(&nama2, &user2)).await? {
        None => Err(key_tidak_ada(&nama)),
        Some(false) => {
            Err(galat(StatusCode::NOT_FOUND, "user_not_found", format!("Client '{user}' tidak punya pengaturan khusus di key '{nama}'.")))
        }
        Some(true) => Ok(Json(json!({ "dihapus": user }))),
    }
}

async fn status_upstream(State(s): State<AppState>) -> Json<Value> {
    let rt = s.runtime();
    let sekarang = std::time::Instant::now();
    let mut daftar = Vec::new();
    for alias in &rt.config.urutan_alias {
        let model = &rt.config.models[alias];
        for (urutan, u) in model.upstreams.iter().enumerate() {
            let st = s.kesehatan.status(&u.id, sekarang);
            daftar.push(json!({
                "alias": alias,
                "urutan": urutan + 1,
                "name": u.name,
                "model": u.model,
                "url": url_aman(u.url.trim_end_matches("/chat/completions")),
                "key_env": u.key_env,
                "terkonfigurasi": u.key_env.is_none() || u.api_key.is_some(),
                "timeout_detik": u.timeout.as_secs(),
                "gagal_beruntun": st.gagal_beruntun,
                "dalam_cooldown": st.sisa_cooldown.is_some(),
                "sisa_cooldown_detik": st.sisa_cooldown.map(|d| d.as_secs_f64().ceil() as u64),
            }));
        }
    }
    Json(json!({ "upstreams": daftar }))
}

async fn statistik(State(s): State<AppState>, RawQuery(raw): RawQuery) -> Hasil {
    let q = query(raw);
    let jam = angka(&q, "jam", 24, 1, MAKS_JAM)?;
    let per = q.get("per").map_or("semua", String::as_str);
    let kelompok = Kelompok::dari_teks(per).map_err(|e| galat(StatusCode::BAD_REQUEST, "invalid_query", e.to_string()))?;
    let sampai = sekarang_ms() + 1;
    let dari = sampai - jam as i64 * 3_600_000;

    let stat = s.statistik.clone();
    let baris = tokio::task::spawn_blocking(move || stat.ringkasan(dari, sampai, kelompok))
        .await
        .map_err(galat_internal)?
        .map_err(galat_internal)?;
    Ok(Json(json!({
        "aktif": s.runtime().config.stats_enabled,
        "jam": jam, "per": per, "dari_ms": dari, "sampai_ms": sampai,
        "dibuang": s.statistik.jumlah_dibuang(),
        "baris": baris,
    })))
}

async fn kejadian_guardrail(State(s): State<AppState>, RawQuery(raw): RawQuery) -> Hasil {
    let q = query(raw);
    let jam = angka(&q, "jam", 24, 1, MAKS_JAM)?;
    let batas = angka(&q, "limit", 100, 1, 1000)? as u32;
    let dari = sekarang_ms() - jam as i64 * 3_600_000;
    let stat = s.statistik.clone();
    let kejadian =
        tokio::task::spawn_blocking(move || stat.temuan_terbaru(dari, batas)).await.map_err(galat_internal)?.map_err(galat_internal)?;
    Ok(Json(json!({ "jam": jam, "kejadian": kejadian })))
}

async fn config_efektif(State(s): State<AppState>) -> Json<Value> {
    let rt = s.runtime();
    let c = &rt.config;
    let g = &c.guardrail;
    let aksi: HashMap<&String, &str> = g.aksi.iter().map(|(k, m)| (k, m.teks())).collect();
    let models: Vec<Value> = c
        .urutan_alias
        .iter()
        .map(|a| {
            json!({
                "alias": a,
                "upstreams": c.models[a].upstreams.iter().map(|u| json!({
                    "name": u.name, "model": u.model, "url": url_aman(u.url.trim_end_matches("/chat/completions")),
                    "key_env": u.key_env, "timeout_detik": u.timeout.as_secs(),
                })).collect::<Vec<_>>(),
            })
        })
        .collect();
    Json(json!({
        "server": { "listen": c.listen, "max_body_mb": c.max_body_bytes / (1024 * 1024) },
        "auth": { "required": c.auth_required },
        "limits": { "default_rpm": c.default_rpm, "default_tpm": c.default_tpm },
        "resilience": {
            "max_retries": c.max_retries, "retry_backoff_ms": c.retry_backoff.as_millis() as u64,
            "cooldown_secs": c.cooldown.as_secs(), "total_timeout_secs": c.total_timeout.as_secs(),
        },
        "stats": { "enabled": c.stats_enabled, "retention_days": c.stats_retention_days },
        "guardrail": {
            "enabled": g.aktif, "mode": g.mode.teks(), "scan_request": g.scan_request, "scan_response": g.scan_response,
            "entropy": g.entropi, "entropy_min_length": g.entropi_min_panjang, "entropy_threshold": g.entropi_ambang,
            "aksi": aksi,
            "rule_kustom": g.kustom.iter().map(|k| json!({ "name": k.nama, "mode": k.mode.map(|m| m.teks()) })).collect::<Vec<_>>(),
        },
        "admin": { "listen": c.admin_listen },
        "models": models,
    }))
}

/// Membaca ulang file config (dengan setting platform terkini) dan menerapkannya. Config tidak valid ditolak seluruhnya;
/// mengembalikan jumlah model dan bagian yang baru berlaku setelah restart. Dipakai API admin dan pemantau platform.
pub fn muat_ulang_config(s: &AppState) -> anyhow::Result<(usize, Vec<&'static str>)> {
    let path = s.config_path.clone().ok_or_else(|| anyhow::anyhow!("gateway tidak dijalankan dari file config"))?;
    let baru = Config::from_file_dengan(&path, &s.platform.pencari())?;
    let (efektif, perlu_restart) = s.runtime().config.gabung_hot(baru);
    let jumlah_model = efektif.models.len();
    s.ganti_config(efektif)?;
    Ok((jumlah_model, perlu_restart))
}

/// Membaca ulang file config dan menerapkannya tanpa restart. Config yang tidak valid ditolak seluruhnya (config lama
/// tetap berjalan). Bagian yang terikat resource terbuka dilaporkan di `perlu_restart` dan tidak berubah.
async fn muat_ulang(State(s): State<AppState>) -> Hasil {
    let Some(path) = s.config_path.clone() else {
        return Err(galat(StatusCode::CONFLICT, "reload_unavailable", "Gateway tidak dijalankan dari file config; reload tidak tersedia."));
    };
    let _ = path;
    let (jumlah_model, perlu_restart) = tokio::task::spawn_blocking(move || muat_ulang_config(&s))
        .await
        .map_err(|_| galat(StatusCode::INTERNAL_SERVER_ERROR, "internal", "Pemuatan ulang config gagal."))?
        .map_err(|e| galat(StatusCode::BAD_REQUEST, "config_invalid", format!("Config tidak valid, tidak diterapkan: {e:#}")))?;
    tracing::info!(?perlu_restart, jumlah_model, "config dimuat ulang lewat API admin");
    Ok(Json(json!({ "status": "ok", "jumlah_model": jumlah_model, "perlu_restart": perlu_restart })))
}
