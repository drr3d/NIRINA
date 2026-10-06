use std::{
    collections::{BTreeMap, HashMap},
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
const KOLOM: &str = "id, name, key_prefix, active, created_at, rpm, tpm, metadata, user_rpm, user_tpm, user_required";
const KOLOM_CLIENT: &str = "key_id, user, rpm, tpm, active, created_at";
const AWALAN_KEY: &str = "ngk_";

/// Label bebas per key (mis. `client_id`) untuk aplikasi di depan gateway. Gateway tidak menafsirkannya; aplikasi membacanya
/// lewat `GET /v1/key/info` dengan key itu sendiri. Urutan terurut supaya keluaran JSON stabil.
pub type Metadata = BTreeMap<String, String>;

pub const MAKS_METADATA: usize = 16;
pub const MAKS_NILAI_METADATA: usize = 256;

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
    pub metadata: Metadata,
    /// Batas bawaan per client (label `user` di request) di bawah key ini; None = client tidak dibatasi sendiri-sendiri.
    pub user_rpm: Option<u64>,
    pub user_tpm: Option<u64>,
    /// Request tanpa label `user` ditolak (supaya semua pemakaian key ini teratribusi ke client).
    pub user_required: bool,
}

/// Pengaturan khusus satu client di bawah sebuah key. Tanpa baris ini client memakai batas bawaan per client dari key.
#[derive(Debug, Clone, PartialEq)]
pub struct Client {
    pub key_id: i64,
    pub user: String,
    /// None = ikut batas bawaan per client dari key.
    pub rpm: Option<u64>,
    pub tpm: Option<u64>,
    /// false = client diblokir (403).
    pub active: bool,
    pub created_at: i64,
}

fn baris_ke_client(r: &rusqlite::Row) -> rusqlite::Result<Client> {
    let batas = |i: usize| -> rusqlite::Result<Option<u64>> { Ok(r.get::<_, Option<i64>>(i)?.map(|v| v.max(0) as u64)) };
    Ok(Client {
        key_id: r.get(0)?,
        user: r.get(1)?,
        rpm: batas(2)?,
        tpm: batas(3)?,
        active: r.get::<_, i64>(4)? != 0,
        created_at: r.get(5)?,
    })
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
        metadata: baca_metadata(r.get::<_, Option<String>>(7)?.as_deref(), r.get::<_, String>(1)?.as_str()),
        user_rpm: batas(8)?,
        user_tpm: batas(9)?,
        user_required: r.get::<_, i64>(10)? != 0,
    })
}

/// Metadata di database selalu ditulis gateway sendiri dalam bentuk valid; isi rusak (diedit manual) dianggap kosong dan
/// dicatat, bukan membuat seluruh daftar key gagal dimuat.
fn baca_metadata(teks: Option<&str>, nama: &str) -> Metadata {
    match teks.map(serde_json::from_str::<Metadata>) {
        None => Metadata::new(),
        Some(Ok(m)) => m,
        Some(Err(e)) => {
            tracing::error!(key = %nama, "metadata key di database rusak ({e}); dianggap kosong");
            Metadata::new()
        }
    }
}

/// Penyimpanan virtual key: SQLite sebagai sumber kebenaran, cache memori untuk jalur request
/// (autentikasi tidak menyentuh disk). Key asli tidak pernah disimpan, hanya hash SHA-256-nya.
pub struct KeyStore {
    conn: Mutex<Connection>,
    /// digest SHA-256 key aktif -> info. Berkunci digest mentah (bukan teks hex) supaya jalur autentikasi tanpa alokasi string.
    cache: RwLock<HashMap<[u8; 32], Arc<KeyInfo>>>,
    /// (key_id, label client) -> pengaturan khusus client. Dibaca di jalur request tanpa menyentuh disk.
    clients: RwLock<HashMap<(i64, String), Arc<Client>>>,
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
        let s = Self {
            conn: Mutex::new(conn),
            cache: RwLock::new(HashMap::new()),
            clients: RwLock::new(HashMap::new()),
            versi_data: AtomicI64::new(-1),
        };
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
        Ok((
            KeyInfo {
                id,
                name: name.to_string(),
                prefix,
                active: true,
                created_at: waktu,
                rpm: None,
                tpm: None,
                metadata: Metadata::new(),
                user_rpm: None,
                user_tpm: None,
                user_required: false,
            },
            token,
        ))
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

    /// Mengganti seluruh metadata sebuah key (kosong = hapus). false bila nama tidak ada.
    pub fn set_metadata(&self, name: &str, metadata: &Metadata) -> Result<bool> {
        validasi_metadata(metadata)?;
        let teks = if metadata.is_empty() { None } else { Some(serde_json::to_string(metadata)?) };
        let conn = kunci(&self.conn);
        let n = conn.execute("UPDATE api_keys SET metadata = ?1 WHERE name = ?2", params![teks, name])?;
        self.muat_dengan(&conn)?;
        Ok(n > 0)
    }

    /// Menetapkan batas bawaan per client dan kewajiban label `user` untuk sebuah key. false bila nama tidak ada.
    pub fn set_aturan_client(&self, name: &str, user_rpm: Option<u64>, user_tpm: Option<u64>, user_required: bool) -> Result<bool> {
        cek_batas(&[("user_rpm", user_rpm), ("user_tpm", user_tpm)])?;
        let conn = kunci(&self.conn);
        let n = conn.execute(
            "UPDATE api_keys SET user_rpm = ?1, user_tpm = ?2, user_required = ?3 WHERE name = ?4",
            params![user_rpm.map(|v| v as i64), user_tpm.map(|v| v as i64), user_required as i64, name],
        )?;
        self.muat_dengan(&conn)?;
        Ok(n > 0)
    }

    /// Membuat atau mengganti pengaturan khusus satu client di bawah key `name`. None bila key tidak ada.
    pub fn set_client(&self, name: &str, user: &str, rpm: Option<u64>, tpm: Option<u64>, active: bool) -> Result<Option<Client>> {
        validasi_user(user)?;
        cek_batas(&[("rpm", rpm), ("tpm", tpm)])?;
        let conn = kunci(&self.conn);
        let Some(key_id) = id_key(&conn, name)? else { return Ok(None) };
        conn.execute(
            "INSERT INTO end_users (key_id, user, rpm, tpm, active, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)
             ON CONFLICT (key_id, user) DO UPDATE SET rpm = excluded.rpm, tpm = excluded.tpm, active = excluded.active",
            params![key_id, user, rpm.map(|v| v as i64), tpm.map(|v| v as i64), active as i64, sekarang()],
        )?;
        self.muat_dengan(&conn)?;
        let c = conn.query_row(
            &format!("SELECT {KOLOM_CLIENT} FROM end_users WHERE key_id = ?1 AND user = ?2"),
            params![key_id, user],
            baris_ke_client,
        )?;
        Ok(Some(c))
    }

    /// Menghapus pengaturan khusus client (client kembali ke batas bawaan). None bila key tidak ada, Some(false) bila
    /// client tidak punya pengaturan khusus.
    pub fn remove_client(&self, name: &str, user: &str) -> Result<Option<bool>> {
        let conn = kunci(&self.conn);
        let Some(key_id) = id_key(&conn, name)? else { return Ok(None) };
        let n = conn.execute("DELETE FROM end_users WHERE key_id = ?1 AND user = ?2", params![key_id, user])?;
        self.muat_dengan(&conn)?;
        Ok(Some(n > 0))
    }

    /// Semua client berpengaturan khusus di bawah key `name`, urut label. None bila key tidak ada.
    pub fn list_clients(&self, name: &str) -> Result<Option<Vec<Client>>> {
        let conn = kunci(&self.conn);
        let Some(key_id) = id_key(&conn, name)? else { return Ok(None) };
        let mut st = conn.prepare(&format!("SELECT {KOLOM_CLIENT} FROM end_users WHERE key_id = ?1 ORDER BY user"))?;
        let daftar = st.query_map(params![key_id], baris_ke_client)?.collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(Some(daftar))
    }

    /// Pengaturan khusus client dari cache memori (tanpa disk). None = client memakai batas bawaan key.
    pub fn client(&self, key_id: i64, user: &str) -> Option<Arc<Client>> {
        // Kunci peta berupa (i64, String): pencarian butuh String milik sendiri; label pendek (maks 64), jadi murah.
        baca(&self.clients).get(&(key_id, user.to_string())).cloned()
    }

    /// Menghapus key permanen beserta pengaturan client-nya (riwayat statistik tetap ada karena menyimpan nama key). false
    /// bila nama tidak ada.
    pub fn remove(&self, name: &str) -> Result<bool> {
        let conn = kunci(&self.conn);
        let Some(key_id) = id_key(&conn, name)? else { return Ok(false) };
        conn.execute("DELETE FROM end_users WHERE key_id = ?1", params![key_id])?;
        let n = conn.execute("DELETE FROM api_keys WHERE id = ?1", params![key_id])?;
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
        for baris in st.query_map([], |r| Ok((r.get::<_, String>(11)?, baris_ke_info(r)?)))? {
            let (hash, info) = baris?;
            match dari_hex32(&hash) {
                Some(d) => {
                    baru.insert(d, Arc::new(info));
                }
                None => tracing::error!(key = %info.name, "hash key di database rusak; key ini dilewati"),
            }
        }
        let mut st = conn.prepare(&format!("SELECT {KOLOM_CLIENT} FROM end_users"))?;
        let clients = st
            .query_map([], baris_ke_client)?
            .map(|c| c.map(|c| ((c.key_id, c.user.clone()), Arc::new(c))))
            .collect::<rusqlite::Result<HashMap<_, _>>>()?;
        *tulis(&self.cache) = baru;
        *tulis(&self.clients) = clients;
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
    // Tambahan aditif TANPA menaikkan user_version: rilis sebelumnya menolak database berversi lebih baru, padahal kolom dan
    // tabel tambahan ini tidak mengganggunya. Dengan begini rollback ke TAG lama tetap bisa membuka database yang sama.
    for (kolom, tipe) in
        [("metadata", "TEXT"), ("user_rpm", "INTEGER"), ("user_tpm", "INTEGER"), ("user_required", "INTEGER NOT NULL DEFAULT 0")]
    {
        let ada: bool =
            conn.query_row("SELECT COUNT(*) > 0 FROM pragma_table_info('api_keys') WHERE name = ?1", params![kolom], |r| r.get(0))?;
        if !ada {
            conn.execute(&format!("ALTER TABLE api_keys ADD COLUMN {kolom} {tipe}"), [])?;
        }
    }
    conn.execute(
        "CREATE TABLE IF NOT EXISTS end_users (
             key_id     INTEGER NOT NULL,
             user       TEXT NOT NULL,
             rpm        INTEGER,
             tpm        INTEGER,
             active     INTEGER NOT NULL DEFAULT 1,
             created_at INTEGER NOT NULL,
             PRIMARY KEY (key_id, user)
         )",
        [],
    )?;
    Ok(())
}

fn id_key(conn: &Connection, name: &str) -> Result<Option<i64>> {
    let mut st = conn.prepare_cached("SELECT id FROM api_keys WHERE name = ?1")?;
    Ok(st.query_map(params![name], |r| r.get(0))?.next().transpose()?)
}

fn cek_batas(daftar: &[(&str, Option<u64>)]) -> Result<()> {
    for (label, v) in daftar {
        if v.is_some_and(|x| x == 0 || x > i64::MAX as u64) {
            bail!("{label} harus >= 1 (atau kosong untuk tanpa batas)");
        }
    }
    Ok(())
}

/// Label client (field `user` request): 1-64 karakter dari huruf, angka, dan `_ - . : @`.
pub fn validasi_user(user: &str) -> Result<()> {
    let ok =
        !user.is_empty() && user.len() <= 64 && user.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.' | ':' | '@'));
    if !ok {
        bail!("label client (field user) harus 1-64 karakter dari huruf, angka, '_', '-', '.', ':', '@'");
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

/// Nama metadata seperti nama key (huruf, angka, `_ - .`, 1-64 karakter); nilai teks 0-256 karakter tanpa karakter kontrol.
pub fn validasi_metadata(m: &Metadata) -> Result<()> {
    if m.len() > MAKS_METADATA {
        bail!("metadata maksimal {MAKS_METADATA} entri");
    }
    for (k, v) in m {
        if k.is_empty() || k.len() > 64 || !k.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.')) {
            bail!("nama metadata '{k}' tidak valid: 1-64 karakter dari huruf, angka, '_', '-', '.'");
        }
        if v.chars().count() > MAKS_NILAI_METADATA || v.chars().any(char::is_control) {
            bail!("nilai metadata '{k}' tidak valid: maksimal {MAKS_NILAI_METADATA} karakter, tanpa baris baru/karakter kontrol");
        }
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
