use std::{
    sync::{
        Arc, RwLock,
        atomic::{AtomicBool, Ordering},
    },
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
            // Redirect tidak diikuti: reqwest akan mengulang body POST (berisi prompt) ke alamat yang ditunjuk upstream.
            .redirect(reqwest::redirect::Policy::none())
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

/// Mengembalikan estimasi TPM yang sudah dipakai bila request berhenti sebelum selesai. Contohnya klien menutup koneksi:
/// handler dibatalkan di tengah `await` dan kode sesudahnya tidak pernah berjalan, jadi pengembalian dilakukan lewat `Drop`.
/// Dilepas (`lepas`) begitu jatah sudah dikoreksi dengan `usage` asli.
struct JatahGuard {
    limiter: Arc<Limiter>,
    key_id: i64,
    estimasi: u64,
    aktif: AtomicBool,
}

impl JatahGuard {
    fn baru(limiter: &Arc<Limiter>, key_id: i64, estimasi: u64) -> Self {
        Self { limiter: Arc::clone(limiter), key_id, estimasi, aktif: AtomicBool::new(true) }
    }

    fn kembalikan(&self) {
        if self.aktif.swap(false, Ordering::SeqCst) {
            self.limiter.koreksi_token(self.key_id, -ke_i64(self.estimasi));
        }
    }

    fn lepas(&self) {
        self.aktif.store(false, Ordering::SeqCst);
    }
}

impl Drop for JatahGuard {
    fn drop(&mut self) {
        self.kembalikan();
    }
}

struct Konteks<'a> {
    s: &'a AppState,
    rt: &'a Arc<Runtime>,
    ident: &'a Identitas,
    jejak: &'a Jejak,
    alias: &'a str,
    estimasi: u64,
    jatah: JatahGuard,
}

fn respons_rusak() -> ApiError {
    ApiError::new(StatusCode::BAD_GATEWAY, "api_error", "upstream_invalid_response", "Respons upstream bukan JSON yang valid.")
}

impl Konteks<'_> {
    /// Satu kali parse untuk usage (statistik + koreksi TPM) dan guardrail respons. Respons 2xx yang bukan JSON adalah upstream
    /// rusak (mis. halaman portal dari `base_url` yang salah): dijawab 502 dan jatah dikembalikan, tidak pernah dianggap sukses.
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
        let Some((usage, lap, baru)) = hasil else {
            tracing::warn!(alias = %self.alias, key = %self.ident.nama, "respons upstream 2xx bukan JSON yang valid");
            self.jatah.kembalikan();
            return Err(respons_rusak());
        };

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
        // 401/403 dari provider berarti key provider di gateway salah; isi galatnya (bisa memuat potongan key atau info akun)
        // tidak boleh sampai ke klien.
        Gagal::Http { status, .. } if matches!(status.as_u16(), 401 | 403) => {
            tracing::error!(status = status.as_u16(), "provider menolak kredensial gateway; periksa key provider");
            (
                StatusCode::BAD_GATEWAY,
                "upstream_auth_failed",
                "Upstream menolak kredensial gateway (periksa key provider di sisi operator).",
            )
        }
        Gagal::Http { status, content_type, body, .. } => return balasan(status, content_type, None, percobaan, body),
        Gagal::Timeout => (StatusCode::GATEWAY_TIMEOUT, "upstream_timeout", "Upstream melewati batas waktu."),
        Gagal::Sambung => (StatusCode::BAD_GATEWAY, "upstream_unreachable", "Upstream tidak dapat dihubungi."),
        Gagal::Terputus => (StatusCode::BAD_GATEWAY, "upstream_read_failed", "Respons upstream terputus."),
        Gagal::TerlaluBesar => (StatusCode::BAD_GATEWAY, "upstream_response_too_large", "Respons upstream melebihi batas ukuran."),
        Gagal::ResponsRusak => return Err(respons_rusak()),
        Gagal::Redirect => (StatusCode::BAD_GATEWAY, "upstream_redirect", "Upstream mengarahkan ulang request (periksa base_url)."),
    };
    Err(ApiError::new(status, "api_error", kode, pesan))
}

/// Parameter yang tidak boleh diteruskan: `stream` (gateway hanya melayani non-streaming, jadi upstream selalu non-streaming)
/// dan kunci pengalih rute milik agregator seperti OpenRouter, yang bisa menimpa model/penyedia yang dipatok di config.
const KUNCI_PENGALIH_RUTE: [&str; 5] = ["provider", "models", "route", "transforms", "plugins"];

fn bersihkan_parameter(req: &mut Value) {
    if let Some(o) = req.as_object_mut() {
        o.remove("stream");
        for k in KUNCI_PENGALIH_RUTE {
            o.remove(k);
        }
    }
}

async fn chat_completions(
    State(s): State<AppState>,
    Extension(ident): Extension<Identitas>,
    Extension(jejak): Extension<Jejak>,
    body: Bytes,
) -> Result<Response, ApiError> {
    // Perkiraan kasar token masukan (~4 byte/token); dikoreksi dengan `usage` asli setelah respons.
    let estimasi = (body.len() as u64 / 4).max(1);
    // Tolak lebih awal bila key sudah melewati batas, TANPA memakai jatah dan sebelum parse/pemindaian yang mahal: request yang
    // pasti ditolak tidak boleh memakan CPU sebesar request yang sukses.
    s.limiter.periksa(ident.key_id, ident.rpm, ident.tpm, estimasi, Instant::now()).map_err(|t| {
        tracing::info!(key = %ident.nama, batas = t.nama(), "request ditolak: rate limit (sebelum parse)");
        ApiError::rate_limited(t)
    })?;

    let besar = body.len() > AMBANG_BERAT;
    let mentah = body.clone();
    let mut req: Value = berat(besar, move || serde_json::from_slice(&mentah))
        .await?
        .map_err(|_| ApiError::bad_request("invalid_json", "Body request bukan JSON yang valid."))?;
    let obj = req.as_object().ok_or_else(|| ApiError::bad_request("invalid_body", "Body request harus berupa objek JSON."))?;
    let alias = obj
        .get("model")
        .and_then(Value::as_str)
        .ok_or_else(|| ApiError::bad_request("missing_model", "Field 'model' wajib diisi (string)."))?
        .to_string();
    // Hanya `false`/`null`/tidak ada yang diterima. Nilai "truthy" lain ("true", 1, ...) bisa dibaca sebagai true oleh server
    // upstream yang longgar, lalu balasan SSE-nya lolos tanpa pemindaian guardrail.
    if !matches!(obj.get("stream"), None | Some(Value::Null) | Some(Value::Bool(false))) {
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

    bersihkan_parameter(&mut req);
    let (mut req, lap_req) = saring_request(&rt, req, besar).await?;
    if lap_req.total() > 0 {
        catat_temuan(&jejak, &lap_req, false);
        tracing::info!(alias = %alias, key = %ident.nama, aturan = ?lap_req.temuan, diblok = !lap_req.diblok.is_empty(), "guardrail: temuan pada request");
        if !lap_req.diblok.is_empty() {
            return Err(ApiError::guardrail_diblok(&lap_req.diblok, false));
        }
    }

    s.limiter.coba(ident.key_id, ident.rpm, ident.tpm, estimasi, Instant::now()).map_err(|t| {
        tracing::info!(alias = %alias, key = %ident.nama, batas = t.nama(), "request ditolak: rate limit");
        ApiError::rate_limited(t)
    })?;
    let ctx = Konteks {
        s: &s,
        rt: &rt,
        ident: &ident,
        jejak: &jejak,
        alias: &alias,
        estimasi,
        jatah: JatahGuard::baru(&s.limiter, ident.key_id, estimasi),
    };

    let akhir = failover::jalankan(&s, &alias, &siap, &mut req, &ident.nama).await;
    kunci(&jejak.0).percobaan = akhir.percobaan;

    match akhir.hasil {
        Ok(b) if !b.status.is_success() => {
            ctx.jatah.kembalikan();
            balasan(b.status, b.content_type, Some(&b.upstream), akhir.percobaan, b.body)
        }
        Ok(b) => {
            let hasil = ctx.proses_sukses(b.body).await;
            // `usage` asli sudah mengoreksi jatah (atau respons diblok setelah token terpakai): jangan dikembalikan lagi.
            ctx.jatah.lepas();
            let isi = hasil?;
            kunci(&jejak.0).upstream = Some(b.upstream.clone());
            balasan(b.status, b.content_type, Some(&b.upstream), akhir.percobaan, isi)
        }
        Err(g) => {
            ctx.jatah.kembalikan();
            galat_upstream(g, akhir.percobaan)
        }
    }
}
