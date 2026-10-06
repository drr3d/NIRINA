//! Integrasi dengan platform (kontrak platform v1): admin mengisi key provider dan token admin di halaman Pengaturan
//! platform, platform menulisnya ke `/platform/config.json` (mount folder, read-only). Setting platform menang atas env
//! proses, kecuali nilainya kosong. Tanpa file ini (laptop, tanpa platform) semua nilai tetap dari env seperti biasa.

use std::{
    collections::HashMap,
    io::ErrorKind,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, SystemTime},
};

use anyhow::{Context, Result, bail};
use serde_json::Value;
use tokio::{task::JoinHandle, time::MissedTickBehavior};

use crate::{proxy::AppState, util::kunci};

/// Env untuk mengganti lokasi file (mis. uji lokal). Bawaannya lokasi mount dari kontrak platform.
pub const ENV_PATH: &str = "NIGATE_PLATFORM_CONFIG";
pub const PATH_BAWAAN: &str = "/platform/config.json";

/// Setting skalar platform sebagai teks (`port` → "9000", `bool` → "true"). Nilai bertingkat/null tidak dimuat.
pub type Settings = HashMap<String, String>;

#[derive(Clone, Copy, PartialEq, Eq)]
struct Sidik {
    ada: bool,
    panjang: u64,
    ubah: Option<SystemTime>,
}

fn sidik(path: &Path) -> Sidik {
    match std::fs::metadata(path) {
        Ok(m) => Sidik { ada: true, panjang: m.len(), ubah: m.modified().ok() },
        Err(_) => Sidik { ada: false, panjang: 0, ubah: None },
    }
}

pub struct SumberPlatform {
    path: PathBuf,
    /// Setting valid terakhir. Platform menulis ulang file kapan saja; kalau sempat terbaca rusak, key yang sedang
    /// dipakai tidak boleh hilang.
    terakhir: Mutex<Option<Arc<Settings>>>,
    sidik: Mutex<Sidik>,
}

impl SumberPlatform {
    pub fn baru(path: impl Into<PathBuf>) -> Self {
        let path = path.into();
        let sidik = sidik(&path);
        Self { path, terakhir: Mutex::new(None), sidik: Mutex::new(sidik) }
    }

    pub fn dari_env() -> Self {
        Self::baru(std::env::var(ENV_PATH).ok().filter(|p| !p.trim().is_empty()).unwrap_or_else(|| PATH_BAWAAN.into()))
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// `Ok(None)` = file belum ada (platform belum pernah menyimpan, atau tidak ada platform). File yang ada tapi rusak
    /// atau versi skemanya lain adalah galat, bukan "kosong".
    pub fn baca(&self) -> Result<Option<Settings>> {
        let isi = match std::fs::read(&self.path) {
            Ok(isi) => isi,
            Err(e) if e.kind() == ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e).with_context(|| format!("tidak bisa membaca config platform {}", self.path.display())),
        };
        parse(&isi).map(Some).with_context(|| format!("config platform {} tidak valid", self.path.display()))
    }

    /// Setting terkini. File rusak → galat dicatat dan setting valid terakhir yang dipakai.
    pub fn settings(&self) -> Option<Arc<Settings>> {
        match self.baca() {
            Ok(s) => {
                let s = s.map(Arc::new);
                kunci(&self.terakhir).clone_from(&s);
                s
            }
            Err(e) => {
                tracing::error!("{e:#}; memakai setting platform valid terakhir");
                kunci(&self.terakhir).clone()
            }
        }
    }

    /// Pencari nilai untuk config: setting platform yang terisi, lalu `env`. Setting dibaca sekali saat dibuat, jadi
    /// satu config selalu dibangun dari satu versi file.
    pub fn pencari_dengan<'a>(&self, env: impl Fn(&str) -> Option<String> + 'a) -> impl Fn(&str) -> Option<String> + 'a {
        let s = self.settings();
        move |nama| {
            s.as_ref().and_then(|m| m.get(nama)).map(|v| v.trim()).filter(|v| !v.is_empty()).map(str::to_string).or_else(|| env(nama))
        }
    }

    pub fn pencari(&self) -> impl Fn(&str) -> Option<String> + use<'_> {
        self.pencari_dengan(|nama| std::env::var(nama).ok())
    }

    /// `true` bila file berubah (isi, waktu ubah, muncul, atau hilang) sejak pemeriksaan sebelumnya.
    pub fn berubah(&self) -> bool {
        let baru = sidik(&self.path);
        let mut lama = kunci(&self.sidik);
        if *lama == baru {
            return false;
        }
        *lama = baru;
        true
    }
}

fn parse(isi: &[u8]) -> Result<Settings> {
    let v: Value = serde_json::from_slice(isi).context("bukan JSON yang valid")?;
    let o = v.as_object().context("isi harus objek JSON")?;
    match o.get("schema_version").and_then(Value::as_u64) {
        Some(1) => {}
        Some(n) => bail!("schema_version {n} tidak didukung (hanya 1)"),
        None => bail!("schema_version tidak ada"),
    }
    let mut hasil = Settings::new();
    for (k, v) in o.get("settings").and_then(Value::as_object).into_iter().flatten() {
        let teks = match v {
            Value::String(s) => s.clone(),
            Value::Number(n) => n.to_string(),
            Value::Bool(b) => b.to_string(),
            _ => continue,
        };
        hasil.insert(k.clone(), teks);
    }
    Ok(hasil)
}

/// Memantau file platform dan menerapkan perubahannya tanpa restart (key provider, model, dst. lewat reload config
/// biasa). Bagian yang butuh restart (token admin) dicatat agar admin menekan Restart di platform.
pub fn pantau(state: AppState, jeda: Duration) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut detak = tokio::time::interval(jeda);
        detak.set_missed_tick_behavior(MissedTickBehavior::Skip);
        loop {
            detak.tick().await;
            if !state.platform.berubah() {
                continue;
            }
            let s = state.clone();
            match tokio::task::spawn_blocking(move || crate::admin::muat_ulang_config(&s)).await {
                Ok(Ok((jumlah_model, perlu_restart))) => {
                    tracing::info!(jumlah_model, "setting platform berubah; config diterapkan ulang");
                    if !perlu_restart.is_empty() {
                        tracing::warn!(?perlu_restart, "sebagian perubahan baru berlaku setelah Restart (tab Service platform)");
                    }
                }
                Ok(Err(e)) => tracing::error!("setting platform berubah tetapi config tidak diterapkan: {e:#}"),
                Err(e) => tracing::error!("pemuatan ulang config gagal: {e}"),
            }
        }
    })
}
