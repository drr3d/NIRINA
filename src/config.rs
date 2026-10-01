use std::{collections::HashMap, time::Duration};

use anyhow::{Context, Result, bail};
use serde::Deserialize;

use crate::guardrail::{AturanKustom, Guardrail, GuardrailCfg, Mode};

#[derive(Debug, Deserialize, Default)]
struct FileConfig {
    #[serde(default)]
    server: FileServer,
    #[serde(default)]
    auth: FileAuth,
    #[serde(default)]
    storage: FileStorage,
    #[serde(default)]
    limits: FileLimits,
    #[serde(default)]
    resilience: FileResilience,
    #[serde(default)]
    stats: FileStats,
    #[serde(default)]
    guardrail: FileGuardrail,
    #[serde(default)]
    admin: FileAdmin,
    #[serde(default, rename = "model")]
    models: Vec<FileModel>,
}

#[derive(Debug, Deserialize)]
struct FileServer {
    #[serde(default = "default_listen")]
    listen: String,
    #[serde(default = "default_max_body_mb")]
    max_body_mb: usize,
    #[serde(default = "default_max_response_mb")]
    max_response_mb: usize,
    #[serde(default = "default_shutdown_grace_secs")]
    shutdown_grace_secs: u64,
}

impl Default for FileServer {
    fn default() -> Self {
        Self {
            listen: default_listen(),
            max_body_mb: default_max_body_mb(),
            max_response_mb: default_max_response_mb(),
            shutdown_grace_secs: default_shutdown_grace_secs(),
        }
    }
}

#[derive(Debug, Deserialize)]
struct FileAuth {
    #[serde(default = "default_true")]
    required: bool,
}

impl Default for FileAuth {
    fn default() -> Self {
        Self { required: true }
    }
}

#[derive(Debug, Deserialize)]
struct FileStorage {
    #[serde(default = "default_db_path")]
    db_path: String,
}

impl Default for FileStorage {
    fn default() -> Self {
        Self { db_path: default_db_path() }
    }
}

#[derive(Debug, Deserialize, Default)]
struct FileLimits {
    default_rpm: Option<u64>,
    default_tpm: Option<u64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileAdmin {
    #[serde(default = "default_true")]
    enabled: bool,
    #[serde(default = "default_admin_listen")]
    listen: String,
    #[serde(default = "default_admin_token_env")]
    token_env: String,
}

impl Default for FileAdmin {
    fn default() -> Self {
        Self { enabled: true, listen: default_admin_listen(), token_env: default_admin_token_env() }
    }
}

fn default_admin_listen() -> String {
    "127.0.0.1:4001".into()
}
fn default_admin_token_env() -> String {
    "NIGATE_ADMIN_TOKEN".into()
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileGuardrail {
    #[serde(default = "default_true")]
    enabled: bool,
    #[serde(default = "default_guardrail_mode")]
    mode: String,
    #[serde(default = "default_true")]
    scan_request: bool,
    #[serde(default = "default_true")]
    scan_response: bool,
    #[serde(default = "default_true")]
    entropy: bool,
    #[serde(default = "default_entropy_min_length")]
    entropy_min_length: usize,
    #[serde(default = "default_entropy_threshold")]
    entropy_threshold: f64,
    #[serde(default)]
    aksi: std::collections::HashMap<String, String>,
    #[serde(default, rename = "rule")]
    rules: Vec<FileGuardrailRule>,
}

impl Default for FileGuardrail {
    fn default() -> Self {
        Self {
            enabled: true,
            mode: default_guardrail_mode(),
            scan_request: true,
            scan_response: true,
            entropy: true,
            entropy_min_length: default_entropy_min_length(),
            entropy_threshold: default_entropy_threshold(),
            aksi: Default::default(),
            rules: Vec::new(),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileGuardrailRule {
    name: String,
    pattern: String,
    mode: Option<String>,
}

fn default_guardrail_mode() -> String {
    "redact".into()
}
fn default_entropy_min_length() -> usize {
    32
}
fn default_entropy_threshold() -> f64 {
    4.5
}

#[derive(Debug, Deserialize)]
struct FileStats {
    #[serde(default = "default_true")]
    enabled: bool,
    #[serde(default = "default_stats_db_path")]
    db_path: String,
    #[serde(default = "default_retention_days")]
    retention_days: u32,
}

impl Default for FileStats {
    fn default() -> Self {
        Self { enabled: true, db_path: default_stats_db_path(), retention_days: default_retention_days() }
    }
}

fn default_stats_db_path() -> String {
    "nigate-stats.db".into()
}
fn default_retention_days() -> u32 {
    30
}

#[derive(Debug, Deserialize)]
struct FileResilience {
    #[serde(default = "default_max_retries")]
    max_retries: u32,
    #[serde(default = "default_retry_backoff_ms")]
    retry_backoff_ms: u64,
    #[serde(default = "default_cooldown_secs")]
    cooldown_secs: u64,
    #[serde(default = "default_total_timeout_secs")]
    total_timeout_secs: u64,
}

impl Default for FileResilience {
    fn default() -> Self {
        Self {
            max_retries: default_max_retries(),
            retry_backoff_ms: default_retry_backoff_ms(),
            cooldown_secs: default_cooldown_secs(),
            total_timeout_secs: default_total_timeout_secs(),
        }
    }
}

fn default_max_retries() -> u32 {
    1
}
fn default_retry_backoff_ms() -> u64 {
    200
}
fn default_cooldown_secs() -> u64 {
    30
}
fn default_total_timeout_secs() -> u64 {
    300
}

#[derive(Debug, Deserialize)]
struct FileModel {
    alias: String,
    #[serde(default)]
    upstream: Vec<FileUpstream>,
}

#[derive(Debug, Deserialize)]
struct FileUpstream {
    base_url: String,
    model: String,
    api_key_env: Option<String>,
    #[serde(default = "default_timeout_secs")]
    timeout_secs: u64,
    /// Nama pendek untuk log dan header x-nigate-upstream (bawaan: upstream-1, upstream-2, ...).
    name: Option<String>,
}

fn default_listen() -> String {
    "127.0.0.1:4000".into()
}
fn default_max_body_mb() -> usize {
    10
}
fn default_max_response_mb() -> usize {
    32
}
fn default_shutdown_grace_secs() -> u64 {
    30
}
fn default_true() -> bool {
    true
}
fn default_db_path() -> String {
    "nigate.db".into()
}
fn default_timeout_secs() -> u64 {
    120
}

/// Konfigurasi hasil validasi, siap dipakai runtime.
#[derive(Debug, Clone)]
pub struct Config {
    pub listen: String,
    pub max_body_bytes: usize,
    /// Batas ukuran body respons upstream yang dibaca ke memori.
    pub max_response_bytes: usize,
    /// Lama menunggu request yang sedang berjalan selesai saat gateway dihentikan.
    pub shutdown_grace: Duration,
    /// Bila true, /v1/* wajib membawa virtual key valid. Matikan hanya untuk pengembangan lokal.
    pub auth_required: bool,
    pub db_path: String,
    /// Batas bawaan untuk key yang belum punya batas sendiri (None = tanpa batas).
    pub default_rpm: Option<u64>,
    pub default_tpm: Option<u64>,
    /// Percobaan ulang di upstream yang sama untuk galat sementara (koneksi, timeout, 5xx).
    pub max_retries: u32,
    pub retry_backoff: Duration,
    /// Lama sebuah upstream dilewati setelah gagal (429 dari provider memakai Retry-After-nya bila ada).
    pub cooldown: Duration,
    /// Batas total waktu satu request termasuk semua percobaan dan failover.
    pub total_timeout: Duration,
    pub guardrail: GuardrailCfg,
    pub admin_enabled: bool,
    pub admin_listen: String,
    /// Token API admin (dibaca dari env). None = API admin tidak dijalankan.
    pub admin_token: Option<String>,
    pub stats_enabled: bool,
    pub stats_db_path: String,
    pub stats_retention_days: u32,
    pub models: HashMap<String, Model>,
    /// Urutan alias sesuai file (untuk /v1/models yang stabil).
    pub urutan_alias: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Model {
    pub upstreams: Vec<Upstream>,
}

#[derive(Debug, Clone)]
pub struct Upstream {
    /// Kunci kesehatan: kombinasi endpoint + model, dipakai bersama semua alias yang menunjuk upstream yang sama.
    pub id: String,
    pub name: String,
    /// URL lengkap endpoint chat completions di upstream.
    pub url: String,
    /// Nama model asli di upstream (menggantikan alias dari klien).
    pub model: String,
    /// Nama env tempat key disimpan (kalau upstream butuh key).
    pub key_env: Option<String>,
    /// Nilai key; None bila upstream tanpa key atau env-nya kosong.
    pub api_key: Option<String>,
    pub timeout: Duration,
}

impl Config {
    pub fn from_file(path: &str) -> Result<Self> {
        let teks = std::fs::read_to_string(path).with_context(|| format!("tidak bisa membaca config {path}"))?;
        Self::from_toml_str(&teks, &|nama| std::env::var(nama).ok())
    }

    /// `env` disuntik supaya bisa diuji tanpa menyentuh environment proses.
    pub fn from_toml_str(teks: &str, env: &dyn Fn(&str) -> Option<String>) -> Result<Self> {
        let f: FileConfig = toml::from_str(teks).context("config TOML tidak valid")?;
        if !(1..=256).contains(&f.server.max_body_mb) {
            bail!("server.max_body_mb harus 1..=256");
        }
        if !(1..=256).contains(&f.server.max_response_mb) {
            bail!("server.max_response_mb harus 1..=256");
        }
        if !(1..=300).contains(&f.server.shutdown_grace_secs) {
            bail!("server.shutdown_grace_secs harus 1..=300");
        }
        let mut models = HashMap::new();
        let mut urutan = Vec::new();
        for m in f.models {
            let alias = m.alias.trim().to_string();
            if alias.is_empty() {
                bail!("model.alias tidak boleh kosong");
            }
            if models.contains_key(&alias) {
                bail!("alias model ganda: {alias}");
            }
            if m.upstream.is_empty() {
                bail!("model '{alias}' belum punya [[model.upstream]]");
            }
            let mut ups = Vec::new();
            for (i, u) in m.upstream.into_iter().enumerate() {
                let base = u.base_url.trim().trim_end_matches('/');
                if !(base.starts_with("http://") || base.starts_with("https://")) {
                    bail!("model '{alias}': base_url harus diawali http:// atau https://");
                }
                if u.model.trim().is_empty() {
                    bail!("model '{alias}': upstream.model tidak boleh kosong");
                }
                if u.timeout_secs == 0 || u.timeout_secs > 600 {
                    bail!("model '{alias}': timeout_secs harus 1..=600");
                }
                let key_env = u.api_key_env.map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
                let api_key = key_env.as_deref().and_then(env).map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
                let nama = match u.name.map(|n| n.trim().to_string()) {
                    Some(n)
                        if n.is_empty()
                            || n.len() > 64
                            || !n.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.')) =>
                    {
                        bail!("model '{alias}': upstream.name harus 1-64 karakter dari huruf, angka, '_', '-', '.'")
                    }
                    Some(n) => n,
                    None => format!("upstream-{}", i + 1),
                };
                if ups.iter().any(|x: &Upstream| x.name == nama) {
                    bail!("model '{alias}': nama upstream ganda: {nama}");
                }
                ups.push(Upstream {
                    id: format!("{base}|{}", u.model.trim()),
                    name: nama,
                    url: format!("{base}/chat/completions"),
                    model: u.model.trim().to_string(),
                    key_env,
                    api_key,
                    timeout: Duration::from_secs(u.timeout_secs),
                });
            }
            urutan.push(alias.clone());
            models.insert(alias, Model { upstreams: ups });
        }
        if f.storage.db_path.trim().is_empty() {
            bail!("storage.db_path tidak boleh kosong");
        }
        for (label, v) in [("default_rpm", f.limits.default_rpm), ("default_tpm", f.limits.default_tpm)] {
            if v.is_some_and(|x| x == 0 || x > i64::MAX as u64) {
                bail!("limits.{label} harus >= 1 (hapus barisnya untuk tanpa batas)");
            }
        }
        let r = &f.resilience;
        if r.max_retries > 5 {
            bail!("resilience.max_retries harus 0..=5");
        }
        if r.retry_backoff_ms > 10_000 {
            bail!("resilience.retry_backoff_ms harus 0..=10000");
        }
        if r.cooldown_secs == 0 || r.cooldown_secs > 3600 {
            bail!("resilience.cooldown_secs harus 1..=3600");
        }
        if r.total_timeout_secs == 0 || r.total_timeout_secs > 1800 {
            bail!("resilience.total_timeout_secs harus 1..=1800");
        }
        if f.stats.db_path.trim().is_empty() {
            bail!("stats.db_path tidak boleh kosong");
        }
        if f.stats.retention_days == 0 || f.stats.retention_days > 3650 {
            bail!("stats.retention_days harus 1..=3650");
        }
        let g = f.guardrail;
        if !(16..=256).contains(&g.entropy_min_length) {
            bail!("guardrail.entropy_min_length harus 16..=256");
        }
        if !(3.0..=6.0).contains(&g.entropy_threshold) {
            bail!("guardrail.entropy_threshold harus 3.0..=6.0");
        }
        let mut aksi = HashMap::new();
        for (nama, m) in &g.aksi {
            aksi.insert(nama.clone(), Mode::dari_teks(m).with_context(|| format!("guardrail.aksi.{nama}"))?);
        }
        let mut kustom = Vec::new();
        for r in g.rules {
            let nama_ok = !r.name.is_empty()
                && r.name.len() <= 40
                && r.name.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_');
            if !nama_ok {
                bail!("guardrail.rule.name '{}' harus 1-40 karakter huruf kecil/angka/_", r.name);
            }
            let mode = r.mode.as_deref().map(Mode::dari_teks).transpose().with_context(|| format!("guardrail.rule '{}'", r.name))?;
            kustom.push(AturanKustom { nama: r.name, pola: r.pattern, mode });
        }
        let guardrail = GuardrailCfg {
            aktif: g.enabled,
            mode: Mode::dari_teks(&g.mode).context("guardrail.mode")?,
            scan_request: g.scan_request,
            scan_response: g.scan_response,
            entropi: g.entropy,
            entropi_min_panjang: g.entropy_min_length,
            entropi_ambang: g.entropy_threshold,
            aksi,
            kustom,
        };
        let admin_token = env(f.admin.token_env.trim()).map(|t| t.trim().to_string()).filter(|t| !t.is_empty());
        if admin_token.as_deref().is_some_and(|t| t.len() < 24) {
            bail!("token admin di env {} terlalu pendek (minimal 24 karakter); buat dengan: nigate admin token", f.admin.token_env.trim());
        }
        Guardrail::baru(&guardrail)?; // validasi lebih awal: pola regex salah / nama aturan typo menggagalkan startup
        Ok(Config {
            guardrail,
            admin_enabled: f.admin.enabled,
            admin_listen: f.admin.listen.trim().to_string(),
            admin_token,
            stats_enabled: f.stats.enabled,
            stats_db_path: f.stats.db_path.trim().to_string(),
            stats_retention_days: f.stats.retention_days,
            max_retries: r.max_retries,
            retry_backoff: Duration::from_millis(r.retry_backoff_ms),
            cooldown: Duration::from_secs(r.cooldown_secs),
            total_timeout: Duration::from_secs(r.total_timeout_secs),
            default_rpm: f.limits.default_rpm,
            default_tpm: f.limits.default_tpm,
            listen: f.server.listen,
            max_body_bytes: f.server.max_body_mb * 1024 * 1024,
            max_response_bytes: f.server.max_response_mb * 1024 * 1024,
            shutdown_grace: Duration::from_secs(f.server.shutdown_grace_secs),
            auth_required: f.auth.required,
            db_path: f.storage.db_path.trim().to_string(),
            models,
            urutan_alias: urutan,
        })
    }
}

impl Config {
    /// Menggabungkan config hasil baca ulang ke yang sedang berjalan. Bagian yang terikat pada resource yang sudah
    /// terbuka (alamat listen, file database, API admin) tidak bisa diganti tanpa restart: nilai lama dipertahankan
    /// dan namanya dilaporkan.
    pub fn gabung_hot(&self, baru: Config) -> (Config, Vec<&'static str>) {
        let mut hasil = baru;
        let mut perlu_restart = Vec::new();
        if hasil.listen != self.listen {
            perlu_restart.push("server.listen");
            hasil.listen = self.listen.clone();
        }
        if hasil.max_body_bytes != self.max_body_bytes {
            perlu_restart.push("server.max_body_mb");
            hasil.max_body_bytes = self.max_body_bytes;
        }
        if hasil.db_path != self.db_path {
            perlu_restart.push("storage.db_path");
            hasil.db_path = self.db_path.clone();
        }
        if (hasil.stats_enabled, &hasil.stats_db_path, hasil.stats_retention_days)
            != (self.stats_enabled, &self.stats_db_path, self.stats_retention_days)
        {
            perlu_restart.push("stats");
            hasil.stats_enabled = self.stats_enabled;
            hasil.stats_db_path = self.stats_db_path.clone();
            hasil.stats_retention_days = self.stats_retention_days;
        }
        if (hasil.admin_enabled, &hasil.admin_listen, &hasil.admin_token) != (self.admin_enabled, &self.admin_listen, &self.admin_token) {
            perlu_restart.push("admin");
            hasil.admin_enabled = self.admin_enabled;
            hasil.admin_listen = self.admin_listen.clone();
            hasil.admin_token = self.admin_token.clone();
        }
        (hasil, perlu_restart)
    }
}
