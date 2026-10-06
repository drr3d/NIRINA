use std::{
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
        mpsc::{self, RecvTimeoutError, SyncSender, TrySendError},
    },
    thread::JoinHandle,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, bail};
use axum::{
    extract::{Request, State},
    middleware::Next,
    response::Response,
};
use rusqlite::{Connection, OpenFlags, params};

use crate::{
    auth::Identitas,
    proxy::AppState,
    util::{ke_i64, kunci},
};

const KAPASITAS_ANTRIAN: usize = 20_000;
const UKURAN_BATCH: usize = 200;
const JEDA_FLUSH: Duration = Duration::from_secs(1);
const JEDA_PURGE: Duration = Duration::from_secs(3600);
const VERSI_SKEMA: i64 = 2;

/// Kode galat dari `ApiError`, dititipkan di extension respons supaya middleware statistik bisa membacanya.
#[derive(Debug, Clone, Copy)]
pub struct KodeGalat(pub &'static str);

/// Rincian yang diisi handler selama memproses request; dibaca middleware setelah respons jadi.
#[derive(Default)]
pub struct Detail {
    pub alias: Option<String>,
    pub upstream: Option<String>,
    pub percobaan: u32,
    pub token_masuk: Option<u64>,
    pub token_keluar: Option<u64>,
    /// Temuan guardrail pada request / respons, dan nama aturan yang terlibat (tanpa isi temuan).
    pub temuan_masuk: u32,
    pub temuan_keluar: u32,
    pub jenis_temuan: std::collections::BTreeSet<String>,
}

#[derive(Clone, Default)]
pub struct Jejak(pub Arc<Mutex<Detail>>);

/// Satu baris statistik. Sengaja TIDAK memuat isi prompt/jawaban, hanya metadata.
#[derive(Debug, Clone)]
pub struct Rekaman {
    pub ts_ms: i64,
    pub key_id: i64,
    pub key_name: String,
    pub alias: Option<String>,
    pub upstream: Option<String>,
    pub status: u16,
    pub hasil: &'static str,
    pub kode_galat: Option<String>,
    pub token_masuk: Option<u64>,
    pub token_keluar: Option<u64>,
    pub latensi_ms: u64,
    pub percobaan: u32,
    pub temuan_masuk: u32,
    pub temuan_keluar: u32,
    /// Nama aturan guardrail yang menemukan sesuatu, dipisah koma.
    pub jenis_temuan: Option<String>,
}

/// Menggolongkan hasil request: ok | klien | limit | guardrail | upstream | gateway.
pub fn klasifikasi(status: u16, kode: Option<&str>) -> &'static str {
    if status < 400 {
        return "ok";
    }
    match kode {
        Some("rate_limit_exceeded") => "limit",
        Some("guardrail_blocked") => "guardrail",
        Some("internal") => "gateway",
        Some(k) if k.starts_with("upstream_") => "upstream",
        Some(_) => "klien",
        // Tanpa kode = respons upstream yang diteruskan apa adanya.
        None if matches!(status, 401 | 403 | 408 | 429) || status >= 500 => "upstream",
        None => "klien",
    }
}

enum Pesan {
    Rekam(Box<Rekaman>),
    Tutup,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Kelompok {
    Semua,
    Key,
    Alias,
    Upstream,
    Hari,
    Jam,
}

impl Kelompok {
    pub fn dari_teks(t: &str) -> Result<Self> {
        Ok(match t {
            "semua" => Self::Semua,
            "key" => Self::Key,
            "alias" => Self::Alias,
            "upstream" => Self::Upstream,
            "hari" => Self::Hari,
            "jam" => Self::Jam,
            lain => bail!("kelompok '{lain}' tidak dikenal (semua|key|alias|upstream|hari|jam)"),
        })
    }

    fn ekspresi_sql(self) -> &'static str {
        match self {
            Self::Semua => "'semua'",
            Self::Key => "key_name",
            Self::Alias => "COALESCE(alias, '-')",
            Self::Upstream => "COALESCE(upstream, '-')",
            Self::Hari => "strftime('%Y-%m-%d', ts / 1000, 'unixepoch')",
            Self::Jam => "strftime('%Y-%m-%d %H:00', ts / 1000, 'unixepoch')",
        }
    }
}

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct Baris {
    pub kelompok: String,
    pub request: u64,
    pub ok: u64,
    pub klien: u64,
    pub limit: u64,
    pub guardrail: u64,
    pub upstream: u64,
    pub gateway: u64,
    /// Total temuan guardrail (request + respons), termasuk yang mode log_only atau di-redact.
    pub temuan: u64,
    pub token_masuk: u64,
    pub token_keluar: u64,
    pub latensi_rata_ms: f64,
    pub latensi_maks_ms: u64,
}

/// Pencatat statistik: request dititipkan ke antrian dan ditulis ke SQLite oleh satu thread pekerja secara
/// berbatch, jadi jalur request tidak pernah menunggu disk. Antrian penuh -> rekaman dibuang (dihitung), bukan
/// menahan request.
pub struct Statistik {
    tx: Option<SyncSender<Pesan>>,
    thread: Mutex<Option<JoinHandle<()>>>,
    dibuang: Arc<AtomicU64>,
    /// Berapa kali pekerja berputar (untuk memastikan ia tidak berputar sia-sia saat idle).
    siklus: Arc<AtomicU64>,
    path: Option<String>,
}

impl Statistik {
    pub fn nonaktif() -> Self {
        Self { tx: None, thread: Mutex::new(None), dibuang: Arc::default(), siklus: Arc::default(), path: None }
    }

    /// Membuka (dan membuat) database statistik, membersihkan data yang lewat masa retensi, lalu menjalankan pekerja.
    pub fn buka(path: &str, retensi_hari: u32) -> Result<Self> {
        let mut conn = Connection::open(path).with_context(|| format!("tidak bisa membuka database statistik {path}"))?;
        conn.busy_timeout(Duration::from_secs(5))?;
        // WAL bila didukung filesystem (beberapa filesystem jaringan/virtual tidak mendukungnya); kalau tidak, tetap jalan dengan mode bawaan.
        match conn.query_row("PRAGMA journal_mode = WAL", [], |r| r.get::<_, String>(0)) {
            Ok(m) if m.eq_ignore_ascii_case("wal") => {}
            Ok(m) => tracing::info!("statistik: journal_mode={m} (WAL tidak tersedia di lokasi ini)"),
            Err(e) => tracing::info!("statistik: WAL tidak dapat diaktifkan: {e}"),
        }
        migrasi(&mut conn)?;
        purge(&conn, retensi_hari)?;

        let (tx, rx) = mpsc::sync_channel::<Pesan>(KAPASITAS_ANTRIAN);
        let dibuang = Arc::new(AtomicU64::new(0));
        let dibuang2 = dibuang.clone();
        let siklus = Arc::new(AtomicU64::new(0));
        let siklus2 = siklus.clone();
        let handle = std::thread::Builder::new()
            .name("nigate-stats".into())
            .spawn(move || pekerja(conn, rx, retensi_hari, dibuang2, siklus2))
            .context("gagal menjalankan thread statistik")?;
        Ok(Self { tx: Some(tx), thread: Mutex::new(Some(handle)), dibuang, siklus, path: Some(path.to_string()) })
    }

    pub fn catat(&self, r: Rekaman) {
        let Some(tx) = &self.tx else { return };
        match tx.try_send(Pesan::Rekam(Box::new(r))) {
            Ok(()) => {}
            Err(TrySendError::Full(_)) => {
                if self.dibuang.fetch_add(1, Ordering::Relaxed).is_multiple_of(1000) {
                    tracing::warn!("antrian statistik penuh: rekaman dibuang (disk lambat?)");
                }
            }
            Err(TrySendError::Disconnected(_)) => {
                // Pekerja sudah berhenti (setelah tutup atau panik): rekaman tidak bisa ditulis, tapi tidak boleh hilang diam-diam.
                self.dibuang.fetch_add(1, Ordering::Relaxed);
            }
        }
    }

    pub fn jumlah_dibuang(&self) -> u64 {
        self.dibuang.load(Ordering::Relaxed)
    }

    pub fn jumlah_siklus_pekerja(&self) -> u64 {
        self.siklus.load(Ordering::Relaxed)
    }

    /// Menulis semua rekaman yang tersisa lalu menghentikan pekerja. Aman dipanggil berulang.
    pub fn tutup(&self) {
        if let Some(tx) = &self.tx {
            let _ = tx.send(Pesan::Tutup);
        }
        if let Some(h) = kunci(&self.thread).take() {
            let _ = h.join();
        }
    }

    /// Request terbaru yang memicu guardrail (metadata saja), terbaru dulu.
    pub fn temuan_terbaru(&self, dari_ms: i64, batas: u32) -> Result<Vec<Kejadian>> {
        match &self.path {
            Some(p) => temuan_terbaru_dari_file(p, dari_ms, batas),
            None => Ok(Vec::new()),
        }
    }

    pub fn ringkasan(&self, dari_ms: i64, sampai_ms: i64, kelompok: Kelompok) -> Result<Vec<Baris>> {
        match &self.path {
            Some(p) => ringkasan_dari_file(p, dari_ms, sampai_ms, kelompok),
            None => Ok(Vec::new()),
        }
    }
}

impl Drop for Statistik {
    fn drop(&mut self) {
        self.tutup();
    }
}

fn pekerja(mut conn: Connection, rx: mpsc::Receiver<Pesan>, retensi_hari: u32, dibuang: Arc<AtomicU64>, siklus: Arc<AtomicU64>) {
    let mut buf: Vec<Rekaman> = Vec::with_capacity(UKURAN_BATCH);
    // Kapan rekaman pertama batch ini masuk. None = idle: menunggu tanpa polling sampai ada rekaman (atau saatnya purge).
    let mut awal_batch: Option<Instant> = None;
    let mut terakhir_purge = Instant::now();
    loop {
        siklus.fetch_add(1, Ordering::Relaxed);
        let tunggu = match awal_batch {
            Some(t) => JEDA_FLUSH.saturating_sub(t.elapsed()),
            None => JEDA_PURGE.saturating_sub(terakhir_purge.elapsed()),
        };
        let selesai = match rx.recv_timeout(tunggu) {
            Ok(Pesan::Rekam(r)) => {
                if buf.is_empty() {
                    awal_batch = Some(Instant::now());
                }
                buf.push(*r);
                false
            }
            Ok(Pesan::Tutup) | Err(RecvTimeoutError::Disconnected) => true,
            Err(RecvTimeoutError::Timeout) => false,
        };
        let jatuh_tempo = awal_batch.is_some_and(|t| t.elapsed() >= JEDA_FLUSH);
        if selesai || buf.len() >= UKURAN_BATCH || jatuh_tempo {
            if let Err(e) = tulis(&mut conn, &buf) {
                tracing::warn!("gagal menulis {} rekaman statistik: {e:#}", buf.len());
                dibuang.fetch_add(buf.len() as u64, Ordering::Relaxed);
            }
            buf.clear();
            awal_batch = None;
        }
        if selesai {
            return;
        }
        if terakhir_purge.elapsed() >= JEDA_PURGE {
            if let Err(e) = purge(&conn, retensi_hari) {
                tracing::warn!("gagal membersihkan statistik lama: {e:#}");
            }
            terakhir_purge = Instant::now();
        }
    }
}

fn tulis(conn: &mut Connection, buf: &[Rekaman]) -> Result<()> {
    if buf.is_empty() {
        return Ok(());
    }
    let tx = conn.transaction()?;
    {
        let mut st = tx.prepare_cached(
            "INSERT INTO requests (ts, key_id, key_name, alias, upstream, status, hasil, kode_galat, token_masuk, token_keluar, latensi_ms, percobaan,
                                   temuan_masuk, temuan_keluar, jenis_temuan)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
        )?;
        for r in buf {
            st.execute(params![
                r.ts_ms,
                r.key_id,
                r.key_name,
                r.alias,
                r.upstream,
                r.status,
                r.hasil,
                r.kode_galat,
                r.token_masuk.map(ke_i64),
                r.token_keluar.map(ke_i64),
                ke_i64(r.latensi_ms),
                r.percobaan,
                r.temuan_masuk,
                r.temuan_keluar,
                r.jenis_temuan
            ])?;
        }
    }
    tx.commit()?;
    Ok(())
}

fn purge(conn: &Connection, retensi_hari: u32) -> Result<()> {
    let batas = sekarang_ms() - retensi_hari as i64 * 86_400_000;
    let n = conn.execute("DELETE FROM requests WHERE ts < ?1", [batas])?;
    if n > 0 {
        tracing::info!("statistik: {n} rekaman lewat masa retensi {retensi_hari} hari dihapus");
    }
    Ok(())
}

fn migrasi(conn: &mut Connection) -> Result<()> {
    let v: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    if v > VERSI_SKEMA {
        bail!("skema database statistik (versi {v}) lebih baru dari yang didukung (versi {VERSI_SKEMA})");
    }
    if v < 1 {
        conn.execute_batch(
            "BEGIN;
             CREATE TABLE requests (
                 id          INTEGER PRIMARY KEY,
                 ts          INTEGER NOT NULL,
                 key_id      INTEGER NOT NULL,
                 key_name    TEXT NOT NULL,
                 alias       TEXT,
                 upstream    TEXT,
                 status      INTEGER NOT NULL,
                 hasil       TEXT NOT NULL,
                 kode_galat  TEXT,
                 token_masuk  INTEGER,
                 token_keluar INTEGER,
                 latensi_ms  INTEGER NOT NULL,
                 percobaan   INTEGER NOT NULL
             );
             CREATE INDEX idx_requests_ts ON requests (ts);
             CREATE INDEX idx_requests_key_ts ON requests (key_id, ts);
             PRAGMA user_version = 1;
             COMMIT;",
        )?;
    }
    if v < 2 {
        conn.execute_batch(
            "BEGIN;
             ALTER TABLE requests ADD COLUMN temuan_masuk INTEGER NOT NULL DEFAULT 0;
             ALTER TABLE requests ADD COLUMN temuan_keluar INTEGER NOT NULL DEFAULT 0;
             ALTER TABLE requests ADD COLUMN jenis_temuan TEXT;
             PRAGMA user_version = 2;
             COMMIT;",
        )?;
    }
    Ok(())
}

/// Membaca ringkasan langsung dari file (untuk CLI `nigate stats` yang berjalan di proses lain).
pub fn ringkasan_dari_file(path: &str, dari_ms: i64, sampai_ms: i64, kelompok: Kelompok) -> Result<Vec<Baris>> {
    if !Path::new(path).exists() {
        bail!("database statistik {path} belum ada (belum ada request tercatat, atau [stats] dimatikan)");
    }
    let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .with_context(|| format!("tidak bisa membuka database statistik {path}"))?;
    conn.busy_timeout(Duration::from_secs(5))?;
    let sql = format!(
        "SELECT {g} AS kel, COUNT(*),
                COALESCE(SUM(hasil = 'ok'), 0), COALESCE(SUM(hasil = 'klien'), 0), COALESCE(SUM(hasil = 'limit'), 0),
                COALESCE(SUM(hasil = 'upstream'), 0), COALESCE(SUM(hasil = 'gateway'), 0),
                COALESCE(SUM(token_masuk), 0), COALESCE(SUM(token_keluar), 0),
                AVG(latensi_ms), MAX(latensi_ms),
                COALESCE(SUM(hasil = 'guardrail'), 0), COALESCE(SUM(temuan_masuk + temuan_keluar), 0)
         FROM requests WHERE ts >= ?1 AND ts < ?2 GROUP BY kel ORDER BY COUNT(*) DESC, kel",
        g = kelompok.ekspresi_sql()
    );
    let mut st = conn.prepare(&sql)?;
    let baris = st
        .query_map([dari_ms, sampai_ms], |r| {
            let n = |i: usize| -> rusqlite::Result<u64> { Ok(r.get::<_, i64>(i)?.max(0) as u64) };
            Ok(Baris {
                kelompok: r.get(0)?,
                request: n(1)?,
                ok: n(2)?,
                klien: n(3)?,
                limit: n(4)?,
                guardrail: n(11)?,
                upstream: n(5)?,
                gateway: n(6)?,
                temuan: n(12)?,
                token_masuk: n(7)?,
                token_keluar: n(8)?,
                latensi_rata_ms: r.get::<_, Option<f64>>(9)?.unwrap_or(0.0),
                latensi_maks_ms: n(10)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(baris)
}

/// Satu request yang memicu guardrail. Tidak memuat isi temuan, hanya jenis aturan dan jumlahnya.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct Kejadian {
    pub ts_ms: i64,
    pub key_name: String,
    pub alias: Option<String>,
    pub status: u16,
    pub hasil: String,
    pub temuan_masuk: u64,
    pub temuan_keluar: u64,
    pub jenis_temuan: Option<String>,
}

pub fn temuan_terbaru_dari_file(path: &str, dari_ms: i64, batas: u32) -> Result<Vec<Kejadian>> {
    if !Path::new(path).exists() {
        return Ok(Vec::new());
    }
    let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .with_context(|| format!("tidak bisa membuka database statistik {path}"))?;
    conn.busy_timeout(Duration::from_secs(5))?;
    let mut st = conn.prepare(
        "SELECT ts, key_name, alias, status, hasil, temuan_masuk, temuan_keluar, jenis_temuan FROM requests
         WHERE ts >= ?1 AND (temuan_masuk + temuan_keluar) > 0 ORDER BY ts DESC, id DESC LIMIT ?2",
    )?;
    let baris = st
        .query_map(params![dari_ms, batas], |r| {
            Ok(Kejadian {
                ts_ms: r.get(0)?,
                key_name: r.get(1)?,
                alias: r.get(2)?,
                status: r.get::<_, i64>(3)?.clamp(0, 999) as u16,
                hasil: r.get(4)?,
                temuan_masuk: r.get::<_, i64>(5)?.max(0) as u64,
                temuan_keluar: r.get::<_, i64>(6)?.max(0) as u64,
                jenis_temuan: r.get(7)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(baris)
}

pub fn sekarang_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// Menitipkan rekaman bila request berhenti sebelum selesai (klien menutup koneksi atau menyerah karena timeout): handler
/// dibatalkan di tengah `await` dan kode sesudah `next.run` tidak pernah berjalan, jadi pencatatan dilakukan dari `Drop`.
struct PencatatRequest {
    statistik: Arc<Statistik>,
    ident: Option<Identitas>,
    jejak: Jejak,
    ts_ms: i64,
    mulai: Instant,
    aktif: bool,
}

impl PencatatRequest {
    fn catat(&mut self, status: u16, kode: Option<&'static str>) {
        self.aktif = false;
        let d = kunci(&self.jejak.0);
        let (key_id, key_name) = self.ident.as_ref().map(|i| (i.key_id, i.nama.clone())).unwrap_or((0, "anonim".into()));
        self.statistik.catat(Rekaman {
            ts_ms: self.ts_ms,
            key_id,
            key_name,
            alias: d.alias.clone(),
            upstream: d.upstream.clone(),
            status,
            hasil: klasifikasi(status, kode),
            kode_galat: kode.map(String::from),
            token_masuk: d.token_masuk,
            token_keluar: d.token_keluar,
            latensi_ms: self.mulai.elapsed().as_millis() as u64,
            percobaan: d.percobaan,
            temuan_masuk: d.temuan_masuk,
            temuan_keluar: d.temuan_keluar,
            jenis_temuan: (!d.jenis_temuan.is_empty()).then(|| d.jenis_temuan.iter().cloned().collect::<Vec<_>>().join(",")),
        });
    }
}

impl Drop for PencatatRequest {
    fn drop(&mut self) {
        if self.aktif {
            // 499 = klien menutup koneksi sebelum jawaban jadi.
            self.catat(499, Some("client_cancelled"));
        }
    }
}

/// Middleware untuk /v1/chat/completions: mengukur latensi dan menitipkan satu rekaman per request (termasuk yang ditolak
/// rate limit, gagal validasi, atau dibatalkan klien). Tidak pernah membaca isi body.
pub async fn catat_statistik(State(s): State<AppState>, mut req: Request, next: Next) -> Response {
    let ident = req.extensions().get::<Identitas>().cloned();
    let jejak = Jejak::default();
    req.extensions_mut().insert(jejak.clone());
    let mut pencatat =
        PencatatRequest { statistik: s.statistik.clone(), ident, jejak, ts_ms: sekarang_ms(), mulai: Instant::now(), aktif: true };

    let resp = next.run(req).await;

    let kode = resp.extensions().get::<KodeGalat>().map(|k| k.0);
    pencatat.catat(resp.status().as_u16(), kode);
    resp
}
