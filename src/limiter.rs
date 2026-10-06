use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use crate::util::kunci;

/// Batas atas waktu tunggu yang dilaporkan, dan juga batas "utang" TPM (dalam menit pengisian): angka `usage` dari
/// upstream tidak tepercaya, jadi satu respons rusak tidak boleh mengunci sebuah key lebih dari sejam.
const MAKS_TUNGGU: Duration = Duration::from_secs(3600);
const MAKS_UTANG_MENIT: f64 = 60.0;

/// Di atas jumlah ember ini, ember client yang sudah penuh kembali dibuang saat client baru masuk. Label client datang dari
/// request, jadi tanpa batas ini pemegang key bisa menumbuhkan peta tanpa henti. Membuang ember penuh tidak mengubah
/// perilaku: client yang datang lagi mendapat ember penuh yang sama.
const AMBANG_SAPU: usize = 50_000;

/// Pemilik sebuah ember: key, atau client (label `user` dari request) di bawah sebuah key.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Subjek {
    Key(i64),
    Client(i64, Arc<str>),
}

/// Satu batas yang harus dipenuhi request: pemiliknya dan batas efektifnya (None = tidak dibatasi).
#[derive(Debug, Clone)]
pub struct Batas {
    pub subjek: Subjek,
    pub rpm: Option<u64>,
    pub tpm: Option<u64>,
}

impl Batas {
    fn kosong(&self) -> bool {
        self.rpm.is_none() && self.tpm.is_none()
    }
}

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

/// Penolakan beserta pemilik batas yang terlampaui.
#[derive(Debug, Clone, PartialEq)]
pub struct Penolakan {
    pub tolak: Tolak,
    pub subjek: Subjek,
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

    fn penuh_pada(&self, now: Instant) -> bool {
        let dt = now.saturating_duration_since(self.terakhir).as_secs_f64();
        self.token + self.kapasitas * dt / 60.0 >= self.kapasitas
    }
}

#[derive(Default)]
struct PerKey {
    rpm: Option<Ember>,
    tpm: Option<Ember>,
}

impl PerKey {
    fn penuh_pada(&self, now: Instant) -> bool {
        self.rpm.as_ref().is_none_or(|e| e.penuh_pada(now)) && self.tpm.as_ref().is_none_or(|e| e.penuh_pada(now))
    }
}

/// Pembatas laju per key dan per client di bawah key (RPM dan TPM), di memori. Keadaan hilang saat restart (bucket kembali
/// penuh).
///
/// TPM bersifat "lunak": sebelum request, saldo harus cukup untuk estimasi token masukan; setelah selesai,
/// saldo dikoreksi dengan pemakaian asli (bisa negatif), sehingga request berikutnya tertahan sampai terisi.
#[derive(Default)]
pub struct Limiter {
    state: Mutex<HashMap<Subjek, PerKey>>,
}

impl Limiter {
    /// Memeriksa semua batas TANPA memakai jatah. Dipakai untuk menolak sedini mungkin, sebelum pekerjaan mahal (parse dan
    /// pemindaian body): request yang pasti ditolak tidak boleh memakan CPU sebesar request yang sukses.
    pub fn periksa_semua(&self, batas: &[Batas], estimasi_token: u64, now: Instant) -> Result<(), Penolakan> {
        let mut peta = kunci(&self.state);
        for b in batas.iter().filter(|b| !b.kosong()) {
            // Pemilik yang belum pernah tercatat punya bucket penuh, jadi pasti lolos.
            if let Some(k) = peta.get_mut(&b.subjek) {
                cek(k, b.rpm, b.tpm, estimasi_token, now).map_err(|tolak| Penolakan { tolak, subjek: b.subjek.clone() })?;
            }
        }
        Ok(())
    }

    /// Memeriksa semua batas lalu memakai jatah di semuanya sekaligus. Kalau salah satu terlampaui, tidak ada jatah yang
    /// terpakai di mana pun.
    pub fn coba_semua(&self, batas: &[Batas], estimasi_token: u64, now: Instant) -> Result<(), Penolakan> {
        let aktif: Vec<&Batas> = batas.iter().filter(|b| !b.kosong()).collect();
        if aktif.is_empty() {
            return Ok(());
        }
        let mut peta = kunci(&self.state);
        if peta.len() >= AMBANG_SAPU && aktif.iter().any(|b| matches!(b.subjek, Subjek::Client(..)) && !peta.contains_key(&b.subjek)) {
            sapu(&mut peta, now);
        }
        for b in &aktif {
            let k = peta.entry(b.subjek.clone()).or_default();
            cek(k, b.rpm, b.tpm, estimasi_token, now).map_err(|tolak| Penolakan { tolak, subjek: b.subjek.clone() })?;
        }
        for b in &aktif {
            let Some(k) = peta.get_mut(&b.subjek) else { continue };
            if let Some(e) = &mut k.rpm {
                e.token -= 1.0;
            }
            if let Some(e) = &mut k.tpm {
                e.token = (e.token - estimasi_token as f64).max(-e.kapasitas * MAKS_UTANG_MENIT);
            }
        }
        Ok(())
    }

    /// Mengoreksi saldo TPM sebuah pemilik: `selisih` positif = memakai lebih banyak dari estimasi, negatif = pengembalian.
    pub fn koreksi(&self, subjek: &Subjek, selisih: i64) {
        if let Some(e) = kunci(&self.state).get_mut(subjek).and_then(|k| k.tpm.as_mut()) {
            e.token = (e.token - selisih as f64).clamp(-e.kapasitas * MAKS_UTANG_MENIT, e.kapasitas);
        }
    }

    /// Sisa jatah saat ini (dibulatkan ke bawah, minimal 0) tanpa memakainya; None untuk batas yang tidak berlaku.
    pub fn sisa_untuk(&self, batas: &Batas, now: Instant) -> (Option<u64>, Option<u64>) {
        let mut peta = kunci(&self.state);
        let Some(k) = peta.get_mut(&batas.subjek) else {
            // Belum pernah dipakai: bucket penuh.
            return (batas.rpm, batas.tpm);
        };
        sinkron(&mut k.rpm, batas.rpm, now);
        sinkron(&mut k.tpm, batas.tpm, now);
        let bulat = |e: &Option<Ember>| e.as_ref().map(|e| e.token.max(0.0).floor() as u64);
        (bulat(&k.rpm), bulat(&k.tpm))
    }

    /// Jumlah ember yang sedang disimpan (key dan client).
    pub fn jumlah_ember(&self) -> usize {
        kunci(&self.state).len()
    }

    // ---- bentuk lama: satu key ----

    pub fn periksa(&self, key_id: i64, rpm: Option<u64>, tpm: Option<u64>, estimasi_token: u64, now: Instant) -> Result<(), Tolak> {
        self.periksa_semua(&[Batas { subjek: Subjek::Key(key_id), rpm, tpm }], estimasi_token, now).map_err(|p| p.tolak)
    }

    pub fn coba(&self, key_id: i64, rpm: Option<u64>, tpm: Option<u64>, estimasi_token: u64, now: Instant) -> Result<(), Tolak> {
        self.coba_semua(&[Batas { subjek: Subjek::Key(key_id), rpm, tpm }], estimasi_token, now).map_err(|p| p.tolak)
    }

    pub fn koreksi_token(&self, key_id: i64, selisih: i64) {
        self.koreksi(&Subjek::Key(key_id), selisih);
    }

    pub fn sisa(&self, key_id: i64, rpm: Option<u64>, tpm: Option<u64>, now: Instant) -> (Option<u64>, Option<u64>) {
        self.sisa_untuk(&Batas { subjek: Subjek::Key(key_id), rpm, tpm }, now)
    }
}

/// Membuang ember client yang sudah penuh kembali (tidak ada utang atau jatah terpakai yang hilang).
fn sapu(peta: &mut HashMap<Subjek, PerKey>, now: Instant) {
    let sebelum = peta.len();
    peta.retain(|s, k| matches!(s, Subjek::Key(_)) || !k.penuh_pada(now));
    tracing::debug!(sebelum, sesudah = peta.len(), "limiter: ember client yang penuh dibuang");
}

/// Menyegarkan bucket sesuai batas saat ini lalu memeriksa apakah request lolos. Tidak memakai jatah.
fn cek(k: &mut PerKey, rpm: Option<u64>, tpm: Option<u64>, estimasi_token: u64, now: Instant) -> Result<(), Tolak> {
    sinkron(&mut k.rpm, rpm, now);
    sinkron(&mut k.tpm, tpm, now);
    if let Some(e) = &k.rpm
        && e.token < 1.0
    {
        return Err(Tolak::Rpm { tunggu: e.tunggu_hingga(1.0) });
    }
    if let Some(e) = &k.tpm {
        // request yang lebih besar dari seluruh kapasitas tetap bisa lewat saat bucket penuh (lalu saldo negatif).
        let butuh = (estimasi_token as f64).min(e.kapasitas);
        if e.token < butuh {
            return Err(Tolak::Tpm { tunggu: e.tunggu_hingga(butuh) });
        }
    }
    Ok(())
}

fn sinkron(slot: &mut Option<Ember>, batas: Option<u64>, now: Instant) {
    match batas {
        Some(b) => slot.get_or_insert_with(|| Ember::baru(b as f64, now)).isi_ulang(b as f64, now),
        None => *slot = None,
    }
}
