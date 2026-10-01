use std::{
    collections::HashMap,
    sync::{
        Arc, Mutex, RwLock,
        atomic::{AtomicI64, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, anyhow, bail};
use rusqlite::{Connection, ErrorCode, params};
use sha2::{Digest, Sha256};

use crate::util::{baca, kunci, tulis};

const VERSI_SKEMA: i64 = 2;
const KOLOM: &str = "id, name, key_prefix, active, created_at, rpm, tpm";
const AWALAN_KEY: &str = "ngk_";

/// Galat bertipe: nama key sudah ada. Pemanggil (API admin) membedakannya dari galat sistem lewat `downcast_ref`.
#[derive(Debug)]
pub struct NamaDipakai(pub String);

impl std::fmt::Display for NamaDipakai {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "nama key '{}' sudah dipakai", self.0)
    }
}

impl std::error::Error for NamaDipakai {}

/// Data key yang aman ditampilkan (tanpa key asli maupun hash-nya).
#[derive(Debug, Clone)]
pub struct KeyInfo {
    pub id: i64,
    pub name: String,
    /// Beberapa karakter awal key, hanya untuk membedakan key di daftar.
    pub prefix: String,
    pub active: bool,
    pub created_at: i64,
    /// Batas request per menit; None = ikut default config (atau tanpa batas).
    pub rpm: Option<u64>,
    /// Batas token per menit; None = ikut default config (atau tanpa batas).
    pub tpm: Option<u64>,
}

fn baris_ke_info(r: &rusqlite::Row) -> rusqlite::Result<KeyInfo> {
    let batas = |i: usize| -> rusqlite::Result<Option<u64>> { Ok(r.get::<_, Option<i64>>(i)?.map(|v| v.max(0) as u64)) };
    Ok(KeyInfo {
        id: r.get(0)?,
        name: r.get(1)?,
        prefix: r.get(2)?,
        active: r.get::<_, i64>(3)? != 0,
        created_at: r.get(4)?,
        rpm: batas(5)?,
        tpm: batas(6)?,
    })
}

/// Penyimpanan virtual key: SQLite sebagai sumber kebenaran, cache memori untuk jalur request
/// (autentikasi tidak menyentuh disk). Key asli tidak pernah disimpan, hanya hash SHA-256-nya.
pub struct KeyStore {
    conn: Mutex<Connection>,
    /// digest SHA-256 key aktif -> info. Berkunci digest mentah (bukan teks hex) supaya jalur autentikasi tanpa alokasi string.
    cache: RwLock<HashMap<[u8; 32], Arc<KeyInfo>>>,
    /// Nilai PRAGMA data_version terakhir yang sudah dimuat (berubah bila proses lain menulis DB).
    versi_data: AtomicI64,
}

impl KeyStore {
    pub fn open(path: &str) -> Result<Self> {
        let conn = Connection::open(path).with_context(|| format!("tidak bisa membuka database {path}"))?;
        Self::dari_koneksi(conn)
    }

    pub fn open_memory() -> Result<Self> {
        Self::dari_koneksi(Connection::open_in_memory()?)
    }

    fn dari_koneksi(conn: Connection) -> Result<Self> {
        conn.busy_timeout(Duration::from_secs(5))?;
        migrasi(&conn)?;
        let s = Self { conn: Mutex::new(conn), cache: RwLock::new(HashMap::new()), versi_data: AtomicI64::new(-1) };
        {
            let conn = kunci(&s.conn);
            s.muat_dengan(&conn)?;
        }
        Ok(s)
    }

    /// Membuat key baru. Key asli hanya dikembalikan di sini, sekali ini saja.
    pub fn create(&self, name: &str) -> Result<(KeyInfo, String)> {
        validasi_nama(name)?;
        let mut acak = [0u8; 32];
        getrandom::fill(&mut acak).map_err(|e| anyhow!("gagal mengambil bilangan acak: {e}"))?;
        let token = format!("{AWALAN_KEY}{}", hex(&acak));
        let prefix = token.chars().take(AWALAN_KEY.len() + 4).collect::<String>();
        let waktu = sekarang();

        let conn = kunci(&self.conn);
        let hasil = conn.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix, active, created_at) VALUES (?1, ?2, ?3, 1, ?4)",
            params![name, hash_key(&token), prefix, waktu],
        );
        match hasil {
            Ok(_) => {}
            Err(rusqlite::Error::SqliteFailure(e, _)) if e.code == ErrorCode::ConstraintViolation => {
                return Err(NamaDipakai(name.to_string()).into());
            }
            Err(e) => return Err(e.into()),
        }
        let id = conn.last_insert_rowid();
        self.muat_dengan(&conn)?;
        Ok((KeyInfo { id, name: name.to_string(), prefix, active: true, created_at: waktu, rpm: None, tpm: None }, token))
    }

    pub fn list(&self) -> Result<Vec<KeyInfo>> {
        let conn = kunci(&self.conn);
        let mut st = conn.prepare(&format!("SELECT {KOLOM} FROM api_keys ORDER BY id"))?;
        let baris = st.query_map([], baris_ke_info)?.collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(baris)
    }

    /// Menetapkan batas RPM/TPM sebuah key (None = hapus batas). false bila nama tidak ada.
    pub fn set_limits(&self, name: &str, rpm: Option<u64>, tpm: Option<u64>) -> Result<bool> {
        for (label, v) in [("rpm", rpm), ("tpm", tpm)] {
            if v == Some(0) || v.is_some_and(|x| x > i64::MAX as u64) {
                bail!("{label} harus >= 1 (atau kosong untuk tanpa batas)");
            }
        }
        let conn = kunci(&self.conn);
        let n = conn.execute(
            "UPDATE api_keys SET rpm = ?1, tpm = ?2 WHERE name = ?3",
            params![rpm.map(|v| v as i64), tpm.map(|v| v as i64), name],
        )?;
        self.muat_dengan(&conn)?;
        Ok(n > 0)
    }

    /// Menghapus key permanen (riwayat statistik tetap ada karena menyimpan nama key). false bila nama tidak ada.
    pub fn remove(&self, name: &str) -> Result<bool> {
        let conn = kunci(&self.conn);
        let n = conn.execute("DELETE FROM api_keys WHERE name = ?1", params![name])?;
        self.muat_dengan(&conn)?;
        Ok(n > 0)
    }

    /// Mengaktifkan/mencabut key. Mengembalikan false bila nama tidak ada.
    pub fn set_active(&self, name: &str, active: bool) -> Result<bool> {
        let conn = kunci(&self.conn);
        let n = conn.execute("UPDATE api_keys SET active = ?1 WHERE name = ?2", params![active as i64, name])?;
        self.muat_dengan(&conn)?;
        Ok(n > 0)
    }

    /// Mengembalikan info key bila token valid dan aktif. Hanya menyentuh cache memori (tanpa disk).
    pub fn authenticate(&self, token: &str) -> Option<Arc<KeyInfo>> {
        baca(&self.cache).get(&digest(token)).cloned()
    }

    /// Info satu key berdasarkan nama (aktif maupun dicabut).
    pub fn get(&self, name: &str) -> Result<Option<KeyInfo>> {
        let conn = kunci(&self.conn);
        let mut st = conn.prepare_cached(&format!("SELECT {KOLOM} FROM api_keys WHERE name = ?1"))?;
        let mut baris = st.query_map(params![name], baris_ke_info)?;
        Ok(baris.next().transpose()?)
    }

    /// Memuat ulang cache bila proses lain (mis. perintah CLI `nigate key ...`) mengubah database.
    pub fn refresh_if_changed(&self) -> Result<()> {
        let conn = kunci(&self.conn);
        if versi_data(&conn)? != self.versi_data.load(Ordering::Relaxed) {
            self.muat_dengan(&conn)?;
        }
        Ok(())
    }

    /// Menjalankan pemantau latar belakang yang memanggil `refresh_if_changed` berkala.
    pub fn pantau_perubahan(self: &Arc<Self>, tiap: Duration) {
        let s = Arc::clone(self);
        tokio::spawn(async move {
            let mut t = tokio::time::interval(tiap);
            loop {
                t.tick().await;
                // Pengecekan menyentuh SQLite (disk): jangan jalankan di worker async.
                let s2 = Arc::clone(&s);
                match tokio::task::spawn_blocking(move || s2.refresh_if_changed()).await {
                    Ok(Ok(())) => {}
                    Ok(Err(e)) => tracing::warn!("gagal memuat ulang key: {e:#}"),
                    Err(e) => tracing::warn!("pemuat ulang key terhenti: {e}"),
                }
            }
        });
    }

    fn muat_dengan(&self, conn: &Connection) -> Result<()> {
        // versi dibaca SEBELUM baris: perubahan yang menyelip di antaranya tertangkap pada pengecekan berikutnya.
        let versi = versi_data(conn)?;
        let mut st = conn.prepare(&format!("SELECT {KOLOM}, key_hash FROM api_keys WHERE active = 1"))?;
        let mut baru = HashMap::new();
        for baris in st.query_map([], |r| Ok((r.get::<_, String>(7)?, baris_ke_info(r)?)))? {
            let (hash, info) = baris?;
            match dari_hex32(&hash) {
                Some(d) => {
                    baru.insert(d, Arc::new(info));
                }
                None => tracing::error!(key = %info.name, "hash key di database rusak; key ini dilewati"),
            }
        }
        *tulis(&self.cache) = baru;
        self.versi_data.store(versi, Ordering::Relaxed);
        Ok(())
    }
}

fn versi_data(conn: &Connection) -> Result<i64> {
    Ok(conn.query_row("PRAGMA data_version", [], |r| r.get(0))?)
}

fn migrasi(conn: &Connection) -> Result<()> {
    let v: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    if v > VERSI_SKEMA {
        bail!("skema database (versi {v}) lebih baru dari yang didukung nigate ini (versi {VERSI_SKEMA})");
    }
    if v < 1 {
        conn.execute_batch(
            "BEGIN;
             CREATE TABLE api_keys (
                 id         INTEGER PRIMARY KEY,
                 name       TEXT NOT NULL UNIQUE,
                 key_hash   TEXT NOT NULL UNIQUE,
                 key_prefix TEXT NOT NULL,
                 active     INTEGER NOT NULL DEFAULT 1,
                 created_at INTEGER NOT NULL
             );
             PRAGMA user_version = 1;
             COMMIT;",
        )?;
    }
    if v < 2 {
        conn.execute_batch(
            "BEGIN;
             ALTER TABLE api_keys ADD COLUMN rpm INTEGER;
             ALTER TABLE api_keys ADD COLUMN tpm INTEGER;
             PRAGMA user_version = 2;
             COMMIT;",
        )?;
    }
    Ok(())
}

pub fn validasi_nama(name: &str) -> Result<()> {
    let ok = !name.is_empty() && name.len() <= 64 && name.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'));
    if !ok {
        bail!("nama key harus 1-64 karakter dari huruf, angka, '_', '-', '.'");
    }
    Ok(())
}

fn digest(token: &str) -> [u8; 32] {
    let mut keluar = [0u8; 32];
    keluar.copy_from_slice(&Sha256::digest(token.as_bytes()));
    keluar
}

/// Hash key dalam bentuk hex (yang disimpan di database).
pub fn hash_key(token: &str) -> String {
    hex(&digest(token))
}

fn hex(b: &[u8]) -> String {
    const HURUF: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(b.len() * 2);
    for &x in b {
        s.push(HURUF[(x >> 4) as usize] as char);
        s.push(HURUF[(x & 15) as usize] as char);
    }
    s
}

fn dari_hex32(t: &str) -> Option<[u8; 32]> {
    if t.len() != 64 || !t.is_ascii() {
        return None;
    }
    let mut keluar = [0u8; 32];
    for (i, pasang) in t.as_bytes().chunks(2).enumerate() {
        keluar[i] = u8::from_str_radix(std::str::from_utf8(pasang).ok()?, 16).ok()?;
    }
    Some(keluar)
}

fn sekarang() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}
