use std::{
    sync::{Arc, RwLock},
    time::{Duration, Instant},
};

use axum::{
    Extension, Json, Router,
    body::{Body, Bytes},
    extract::{DefaultBodyLimit, State},
    http::{HeaderValue, StatusCode, header},
    middleware,
    response::Response,
    routing::{get, post},
};
use serde_json::{Value, json};

use crate::{
    auth::{Identitas, autentikasi},
    config::{Config, Upstream},
    error::ApiError,
    failover::{self, Gagal},
    guardrail::{Guardrail, Laporan},
    kesehatan::Kesehatan,
    keys::KeyStore,
    limiter::Limiter,
    stats::{Jejak, Statistik, catat_statistik},
    util::{baca, ke_i64, kunci, tulis},
};

/// Bagian yang bisa diganti saat reload config: satu potret konsisten (config + guardrail hasil kompilasinya).
/// Setiap request mengambil potret sekali di awal, jadi tidak pernah melihat setengah config lama dan setengah baru.
pub struct Runtime {
    pub config: Config,
    pub guardrail: Guardrail,
}

#[derive(Clone)]
pub struct AppState {
    runtime: Arc<RwLock<Arc<Runtime>>>,
    pub client: reqwest::Client,
    pub keys: Arc<KeyStore>,
    pub limiter: Arc<Limiter>,
    pub kesehatan: Arc<Kesehatan>,
    pub statistik: Arc<Statistik>,
    /// Lokasi file config untuk reload (None = reload tidak tersedia).
    pub config_path: Option<Arc<str>>,
    pub mulai: Instant,
}

impl AppState {
    pub fn new(config: Config, keys: Arc<KeyStore>) -> anyhow::Result<Self> {
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .pool_idle_timeout(Duration::from_secs(90))
            .pool_max_idle_per_host(32)
            .tcp_keepalive(Duration::from_secs(60))
            .tcp_nodelay(true)
            .build()?;
        let guardrail = Guardrail::baru(&config.guardrail)?;
        Ok(Self {
            runtime: Arc::new(RwLock::new(Arc::new(Runtime { config, guardrail }))),
            client,
            keys,
            limiter: Arc::default(),
            kesehatan: Arc::default(),
            statistik: Arc::new(Statistik::nonaktif()),
            config_path: None,
            mulai: Instant::now(),
        })
    }

    /// Potret runtime saat ini (murah: hanya menyalin Arc).
    pub fn runtime(&self) -> Arc<Runtime> {
        baca(&self.runtime).clone()
    }

    /// Mengganti config yang berjalan secara atomik. Gagal (dan config lama tetap dipakai) bila config baru tidak valid.
    pub fn ganti_config(&self, config: Config) -> anyhow::Result<()> {
        let guardrail = Guardrail::baru(&config.guardrail)?;
        *tulis(&self.runtime) = Arc::new(Runtime { config, guardrail });
        Ok(())
    }

    pub fn dengan_config_path(mut self, path: &str) -> Self {
        self.config_path = Some(Arc::from(path));
        self
    }

    pub fn dengan_statistik(mut self, statistik: Arc<Statistik>) -> Self {
        self.statistik = statistik;
        self
    }
}

pub fn app(state: AppState) -> Router {
    let batas = state.runtime().config.max_body_bytes;
    let chat = Router::new()
        .route("/v1/chat/completions", post(chat_completions))
        .layer(middleware::from_fn_with_state(state.clone(), catat_statistik));
    let v1 =
        Router::new().route("/v1/models", get(daftar_model)).merge(chat).layer(middleware::from_fn_with_state(state.clone(), autentikasi));
    Router::new().route("/healthz", get(healthz)).merge(v1).layer(DefaultBodyLimit::max(batas)).with_state(state)
}

/// Pemakaian token dari field `usage` respons OpenAI-compatible.
#[derive(Debug, Default, PartialEq)]
pub struct Usage {
    pub masuk: Option<u64>,
    pub keluar: Option<u64>,
    pub total: Option<u64>,
}

impl Usage {
    /// total_tokens, atau masuk + keluar bila total tidak ada.
    fn jumlah(&self) -> Option<u64> {
        self.total.or_else(|| self.masuk?.checked_add(self.keluar?))
    }
}

pub fn baca_usage(body: &[u8]) -> Option<Usage> {
    usage_dari_value(&serde_json::from_slice::<Value>(body).ok()?)
}

fn usage_dari_value(v: &Value) -> Option<Usage> {
    let u = v.get("usage")?;
    let n = |k: &str| u.get(k).and_then(Value::as_u64);
    Some(Usage { masuk: n("prompt_tokens"), keluar: n("completion_tokens"), total: n("total_tokens") })
}

async fn healthz() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

async fn daftar_model(State(s): State<AppState>) -> Json<Value> {
    let rt = s.runtime();
    let data: Vec<Value> = rt.config.urutan_alias.iter().map(|a| json!({ "id": a, "object": "model", "owned_by": "nigate" })).collect();
    Json(json!({ "object": "list", "data": data }))
}

fn tambah_header_nigate(mut b: axum::http::response::Builder, upstream: Option<&str>, percobaan: u32) -> axum::http::response::Builder {
    if let Some(u) = upstream.and_then(|u| HeaderValue::from_str(u).ok()) {
        b = b.header("x-nigate-upstream", u);
    }
    b.header("x-nigate-attempts", percobaan)
}

/// Di atas ukuran ini, parsing dan pemindaian JSON dipindah ke thread pemblokir supaya tidak menahan worker async.
const AMBANG_BERAT: usize = 64 * 1024;

/// Menjalankan pekerjaan CPU: langsung untuk payload kecil, lewat `spawn_blocking` untuk yang besar.
async fn berat<T, F>(besar: bool, kerja: F) -> Result<T, ApiError>
where
    T: Send + 'static,
    F: FnOnce() -> T + Send + 'static,
{
    if !besar {
        return Ok(kerja());
    }
    tokio::task::spawn_blocking(kerja).await.map_err(|e| {
        tracing::error!("pekerjaan berat gagal: {e}");
        galat_internal()
    })
}

fn galat_internal() -> ApiError {
    ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "api_error", "internal", "Galat internal.")
}

fn catat_temuan(jejak: &Jejak, lap: &Laporan, respons: bool) {
    let mut d = kunci(&jejak.0);
    if respons {
        d.temuan_keluar += lap.total();
    } else {
        d.temuan_masuk += lap.total();
    }
    d.jenis_temuan.extend(lap.temuan.keys().cloned());
}

/// Memindai isi request sebelum jatah rate limit terpakai dan sebelum apa pun keluar ke provider.
async fn saring_request(rt: &Arc<Runtime>, req: Value, besar: bool) -> Result<(Value, Laporan), ApiError> {
    if !rt.guardrail.aktif_request() {
        return Ok((req, Laporan::default()));
    }
    let rt = Arc::clone(rt);
    berat(besar, move || {
        let (mut req, mut lap) = (req, Laporan::default());
        rt.guardrail.pindai_request(&mut req, &mut lap);
        (req, lap)
    })
    .await
}

struct Konteks<'a> {
    s: &'a AppState,
    rt: &'a Arc<Runtime>,
    ident: &'a Identitas,
    jejak: &'a Jejak,
    alias: &'a str,
    estimasi: u64,
}

impl Konteks<'_> {
    fn kembalikan_jatah(&self) {
        self.s.limiter.koreksi_token(self.ident.key_id, -ke_i64(self.estimasi));
    }

    /// Satu kali parse untuk usage (statistik + koreksi TPM) dan guardrail respons. Isi bukan JSON diteruskan apa adanya.
    async fn proses_sukses(&self, isi: Bytes) -> Result<Bytes, ApiError> {
        let (besar, saring) = (isi.len() > AMBANG_BERAT, self.rt.guardrail.aktif_response());
        let (rt, salinan) = (Arc::clone(self.rt), isi.clone());
        let hasil = berat(besar, move || {
            let mut v: Value = serde_json::from_slice(&salinan).ok()?;
            let usage = usage_dari_value(&v);
            let mut lap = Laporan::default();
            let mut baru = None;
            if saring {
                rt.guardrail.pindai_respons(&mut v, &mut lap);
                if lap.berubah {
                    baru = serde_json::to_vec(&v).ok().map(Bytes::from);
                }
            }
            Some((usage, lap, baru))
        })
        .await?;
        let Some((usage, lap, baru)) = hasil else { return Ok(isi) };

        if let Some(u) = usage {
            if self.ident.tpm.is_some()
                && let Some(total) = u.jumlah()
            {
                self.s.limiter.koreksi_token(self.ident.key_id, ke_i64(total).saturating_sub(ke_i64(self.estimasi)));
            }
            let mut d = kunci(&self.jejak.0);
            (d.token_masuk, d.token_keluar) = (u.masuk, u.keluar);
        }
        if lap.total() > 0 {
            catat_temuan(self.jejak, &lap, true);
            tracing::info!(alias = %self.alias, key = %self.ident.nama, aturan = ?lap.temuan, diblok = !lap.diblok.is_empty(), "guardrail: temuan pada respons");
            if !lap.diblok.is_empty() {
                return Err(ApiError::guardrail_diblok(&lap.diblok, true));
            }
        }
        Ok(baru.unwrap_or(isi))
    }
}

fn balasan(
    status: StatusCode,
    content_type: Option<HeaderValue>,
    upstream: Option<&str>,
    percobaan: u32,
    isi: Bytes,
) -> Result<Response, ApiError> {
    let mut out = tambah_header_nigate(Response::builder().status(status), upstream, percobaan);
    if let Some(c) = content_type {
        out = out.header(header::CONTENT_TYPE, c);
    }
    out.body(Body::from(isi)).map_err(|_| galat_internal())
}

fn galat_upstream(g: Gagal, percobaan: u32) -> Result<Response, ApiError> {
    let (status, kode, pesan) = match g {
        Gagal::Http { status, content_type, body, .. } => return balasan(status, content_type, None, percobaan, body),
        Gagal::Timeout => (StatusCode::GATEWAY_TIMEOUT, "upstream_timeout", "Upstream melewati batas waktu."),
        Gagal::Sambung => (StatusCode::BAD_GATEWAY, "upstream_unreachable", "Upstream tidak dapat dihubungi."),
        Gagal::Terputus => (StatusCode::BAD_GATEWAY, "upstream_read_failed", "Respons upstream terputus."),
        Gagal::TerlaluBesar => (StatusCode::BAD_GATEWAY, "upstream_response_too_large", "Respons upstream melebihi batas ukuran."),
    };
    Err(ApiError::new(status, "api_error", kode, pesan))
}

async fn chat_completions(
    State(s): State<AppState>,
    Extension(ident): Extension<Identitas>,
    Extension(jejak): Extension<Jejak>,
    body: Bytes,
) -> Result<Response, ApiError> {
    let besar = body.len() > AMBANG_BERAT;
    let mentah = body.clone();
    let req: Value = berat(besar, move || serde_json::from_slice(&mentah))
        .await?
        .map_err(|_| ApiError::bad_request("invalid_json", "Body request bukan JSON yang valid."))?;
    let obj = req.as_object().ok_or_else(|| ApiError::bad_request("invalid_body", "Body request harus berupa objek JSON."))?;
    let alias = obj
        .get("model")
        .and_then(Value::as_str)
        .ok_or_else(|| ApiError::bad_request("missing_model", "Field 'model' wajib diisi (string)."))?
        .to_string();
    if obj.get("stream").and_then(Value::as_bool) == Some(true) {
        return Err(ApiError::bad_request("stream_unsupported", "Streaming belum didukung gateway ini."));
    }

    let rt = s.runtime();
    let model = rt.config.models.get(&alias).ok_or_else(|| {
        ApiError::new(StatusCode::NOT_FOUND, "invalid_request_error", "model_not_found", format!("Model '{alias}' tidak dikenal gateway."))
    })?;
    // Alias dicatat hanya setelah terbukti dikenal, supaya statistik tidak dikotori nama model sembarang dari klien.
    kunci(&jejak.0).alias = Some(alias.clone());

    // Upstream yang key-nya belum terisi di environment dilewati; kalau tidak ada yang siap, 503.
    let siap: Vec<&Upstream> = model.upstreams.iter().filter(|u| u.key_env.is_none() || u.api_key.is_some()).collect();
    if siap.is_empty() {
        tracing::error!(alias = %alias, "semua upstream belum dikonfigurasi (env key kosong)");
        return Err(ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "api_error",
            "upstream_not_configured",
            "Upstream untuk model ini belum dikonfigurasi di gateway.",
        ));
    }

    let (mut req, lap_req) = saring_request(&rt, req, besar).await?;
    if lap_req.total() > 0 {
        catat_temuan(&jejak, &lap_req, false);
        tracing::info!(alias = %alias, key = %ident.nama, aturan = ?lap_req.temuan, diblok = !lap_req.diblok.is_empty(), "guardrail: temuan pada request");
        if !lap_req.diblok.is_empty() {
            return Err(ApiError::guardrail_diblok(&lap_req.diblok, false));
        }
    }

    // Perkiraan kasar token masukan (~4 byte/token); dikoreksi dengan `usage` asli setelah respons.
    let estimasi = (body.len() as u64 / 4).max(1);
    s.limiter.coba(ident.key_id, ident.rpm, ident.tpm, estimasi, Instant::now()).map_err(|t| {
        tracing::info!(alias = %alias, key = %ident.nama, batas = t.nama(), "request ditolak: rate limit");
        ApiError::rate_limited(t)
    })?;
    let ctx = Konteks { s: &s, rt: &rt, ident: &ident, jejak: &jejak, alias: &alias, estimasi };

    let akhir = failover::jalankan(&s, &alias, &siap, &mut req, &ident.nama).await;
    kunci(&jejak.0).percobaan = akhir.percobaan;

    match akhir.hasil {
        Ok(b) if !b.status.is_success() => {
            ctx.kembalikan_jatah();
            balasan(b.status, b.content_type, Some(&b.upstream), akhir.percobaan, b.body)
        }
        Ok(b) => {
            let isi = ctx.proses_sukses(b.body).await?;
            kunci(&jejak.0).upstream = Some(b.upstream.clone());
            balasan(b.status, b.content_type, Some(&b.upstream), akhir.percobaan, isi)
        }
        Err(g) => {
            ctx.kembalikan_jatah();
            galat_upstream(g, akhir.percobaan)
        }
    }
}
