use std::{
    collections::HashMap,
    sync::Mutex,
    time::{Duration, Instant},
};

use crate::util::kunci;

/// Batas atas waktu tunggu yang dilaporkan, dan juga batas "utang" TPM (dalam menit pengisian): angka `usage` dari
/// upstream tidak tepercaya, jadi satu respons rusak tidak boleh mengunci sebuah key lebih dari sejam.
const MAKS_TUNGGU: Duration = Duration::from_secs(3600);
const MAKS_UTANG_MENIT: f64 = 60.0;

/// Alasan request ditolak beserta perkiraan waktu tunggu sampai bisa dicoba lagi.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Tolak {
    Rpm { tunggu: Duration },
    Tpm { tunggu: Duration },
}

impl Tolak {
    pub fn tunggu(&self) -> Duration {
        match self {
            Tolak::Rpm { tunggu } | Tolak::Tpm { tunggu } => *tunggu,
        }
    }

    /// Nilai header Retry-After (detik, dibulatkan ke atas, minimal 1).
    pub fn retry_after_detik(&self) -> u64 {
        (self.tunggu().as_secs_f64().ceil() as u64).max(1)
    }

    pub fn nama(&self) -> &'static str {
        match self {
            Tolak::Rpm { .. } => "requests per menit (RPM)",
            Tolak::Tpm { .. } => "token per menit (TPM)",
        }
    }
}

/// Token bucket: kapasitas = batas per menit (burst penuh di awal), terisi lagi merata sepanjang menit.
struct Ember {
    token: f64,
    kapasitas: f64,
    terakhir: Instant,
}

impl Ember {
    fn baru(kapasitas: f64, now: Instant) -> Self {
        Self { token: kapasitas, kapasitas, terakhir: now }
    }

    fn isi_ulang(&mut self, kapasitas: f64, now: Instant) {
        let dt = now.saturating_duration_since(self.terakhir).as_secs_f64();
        self.token = (self.token + kapasitas * dt / 60.0).min(kapasitas);
        self.kapasitas = kapasitas;
        self.terakhir = self.terakhir.max(now);
    }

    fn tunggu_hingga(&self, butuh: f64) -> Duration {
        if self.token >= butuh {
            Duration::ZERO
        } else {
            Duration::try_from_secs_f64((butuh - self.token) * 60.0 / self.kapasitas).map_or(MAKS_TUNGGU, |d| d.min(MAKS_TUNGGU))
        }
    }
}

#[derive(Default)]
struct PerKey {
    rpm: Option<Ember>,
    tpm: Option<Ember>,
}

/// Pembatas laju per key (RPM dan TPM), di memori. Keadaan hilang saat restart (bucket kembali penuh).
///
/// TPM bersifat "lunak": sebelum request, saldo harus cukup untuk estimasi token masukan; setelah selesai,
/// saldo dikoreksi dengan pemakaian asli (bisa negatif), sehingga request berikutnya tertahan sampai terisi.
#[derive(Default)]
pub struct Limiter {
    state: Mutex<HashMap<i64, PerKey>>,
}

impl Limiter {
    /// Memeriksa dan sekaligus memakai jatah. Kalau salah satu batas terlewati, tidak ada jatah yang terpakai.
    pub fn coba(&self, key_id: i64, rpm: Option<u64>, tpm: Option<u64>, estimasi_token: u64, now: Instant) -> Result<(), Tolak> {
        if rpm.is_none() && tpm.is_none() {
            return Ok(());
        }
        let mut peta = kunci(&self.state);
        let k = peta.entry(key_id).or_default();
        sinkron(&mut k.rpm, rpm, now);
        sinkron(&mut k.tpm, tpm, now);

        if let Some(e) = &k.rpm
            && e.token < 1.0
        {
            return Err(Tolak::Rpm { tunggu: e.tunggu_hingga(1.0) });
        }
        let estimasi = estimasi_token as f64;
        if let Some(e) = &k.tpm {
            // request yang lebih besar dari seluruh kapasitas tetap bisa lewat saat bucket penuh (lalu saldo negatif).
            let butuh = estimasi.min(e.kapasitas);
            if e.token < butuh {
                return Err(Tolak::Tpm { tunggu: e.tunggu_hingga(butuh) });
            }
        }
        if let Some(e) = &mut k.rpm {
            e.token -= 1.0;
        }
        if let Some(e) = &mut k.tpm {
            e.token = (e.token - estimasi).max(-e.kapasitas * MAKS_UTANG_MENIT);
        }
        Ok(())
    }

    /// Mengoreksi saldo TPM: `selisih` positif = memakai lebih banyak dari estimasi, negatif = pengembalian.
    pub fn koreksi_token(&self, key_id: i64, selisih: i64) {
        if let Some(e) = kunci(&self.state).get_mut(&key_id).and_then(|k| k.tpm.as_mut()) {
            e.token = (e.token - selisih as f64).clamp(-e.kapasitas * MAKS_UTANG_MENIT, e.kapasitas);
        }
    }
}

fn sinkron(slot: &mut Option<Ember>, batas: Option<u64>, now: Instant) {
    match batas {
        Some(b) => slot.get_or_insert_with(|| Ember::baru(b as f64, now)).isi_ulang(b as f64, now),
        None => *slot = None,
    }
}
