//! Pembantu kecil lintas modul.

use std::{
    sync::{
        Mutex, MutexGuard, RwLock, RwLockReadGuard, RwLockWriteGuard,
        atomic::{AtomicU64, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};

/// Mengunci mutex. Bila thread lain sempat panik saat memegangnya, tetap lanjut memakai datanya: keadaan yang dijaga
/// di sini (penghitung, cache, koneksi) aman dipakai ulang, dan lebih baik daripada membuat SETIAP request berikutnya
/// ikut panik karena racun (poison) dari satu kegagalan.
pub fn kunci<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

pub fn baca<T>(l: &RwLock<T>) -> RwLockReadGuard<'_, T> {
    l.read().unwrap_or_else(|e| e.into_inner())
}

pub fn tulis<T>(l: &RwLock<T>) -> RwLockWriteGuard<'_, T> {
    l.write().unwrap_or_else(|e| e.into_inner())
}

/// u64 -> i64 tanpa membalik tanda: nilai di atas i64::MAX dijepit, bukan dibungkus (wrap) menjadi negatif.
/// Penting untuk angka dari luar (mis. `usage` yang dilaporkan upstream).
pub fn ke_i64(v: u64) -> i64 {
    i64::try_from(v).unwrap_or(i64::MAX)
}

/// Pembatas log: mengizinkan satu pesan per selang waktu, supaya kejadian yang bisa dipicu dari luar (mis. tebakan
/// token admin berulang) tidak membanjiri log.
pub struct PembatasLog {
    terakhir_ms: AtomicU64,
    sela_ms: u64,
}

impl PembatasLog {
    pub const fn baru(sela_ms: u64) -> Self {
        Self { terakhir_ms: AtomicU64::new(0), sela_ms }
    }

    pub fn boleh(&self) -> bool {
        self.boleh_pada(SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64))
    }

    /// Terpisah dari jam sistem supaya bisa diuji.
    pub fn boleh_pada(&self, sekarang_ms: u64) -> bool {
        let lama = self.terakhir_ms.load(Ordering::Relaxed);
        sekarang_ms.saturating_sub(lama) >= self.sela_ms
            && self.terakhir_ms.compare_exchange(lama, sekarang_ms, Ordering::Relaxed, Ordering::Relaxed).is_ok()
    }
}
