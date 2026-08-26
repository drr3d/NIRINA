import sqlite3
import json
import uuid
import time
import logging
from typing import List, Dict, Any, Optional

from core_agent.config import sqlite_db_path

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')


def _kolom_ada(conn, tabel: str, kolom: str) -> bool:
    """Cek 1 kolom sudah ada di tabel atau belum -- SQLite lama (<3.35) gak
    dukung 'ADD COLUMN IF NOT EXISTS', jadi kita cek manual lewat PRAGMA."""
    info = conn.execute(f"PRAGMA table_info({tabel})").fetchall()
    return any(row[1] == kolom for row in info)


def init_db():
    """Bikin tabel automations + automation_runs kalau belum ada, DAN migrasi
    kolom baru (last_run_at) ke tabel automations lama tanpa hapus data."""
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute('''
                CREATE TABLE IF NOT EXISTS automations (
                    id TEXT PRIMARY KEY,
                    nama_alur TEXT NOT NULL,
                    status TEXT DEFAULT 'STOPPED',
                    tipe_jadwal TEXT NOT NULL,
                    waktu_eksekusi TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # ---  Migrasi kolom last_run_at ke tabel lama (kalau ada) ---
            # Dipakai daemon buat tau "apa job DAILY jam 09:00 ini udah jalan
            # hari ini belum" / "udah berapa detik sejak job INTERVAL ini
            # terakhir jalan". NULL = belum pernah jalan sama sekali.
            if not _kolom_ada(conn, "automations", "last_run_at"):
                conn.execute("ALTER TABLE automations ADD COLUMN last_run_at TIMESTAMP")
                logging.info("Migrasi: kolom 'last_run_at' ditambahkan ke tabel automations.")

            # ---  Migrasi kolom buat trigger EVENT (chaining antar-automation) ---
            # depends_on_id   : id automation LAIN yang jadi pemicu (NULL kalau
            #                   tipe_jadwal bukan EVENT).
            # depends_on_status: "SUKSES" | "GAGAL" | "ANY" -- status run automation
            #                   pemicu yang bikin automation ini dianggap due.
            if not _kolom_ada(conn, "automations", "depends_on_id"):
                conn.execute("ALTER TABLE automations ADD COLUMN depends_on_id TEXT")
                logging.info("Migrasi: kolom 'depends_on_id' ditambahkan ke tabel automations.")
            if not _kolom_ada(conn, "automations", "depends_on_status"):
                conn.execute("ALTER TABLE automations ADD COLUMN depends_on_status TEXT")
                logging.info("Migrasi: kolom 'depends_on_status' ditambahkan ke tabel automations.")

            # ---  Tabel riwayat eksekusi ---
            # SATU baris = SATU kali automation ini beneran dijalankan daemon.
            # Terpisah dari tabel automations (yang cuma nyimpen definisi/status
            # aktif) supaya histori gak numpuk/nge-bloat tabel utama, dan gampang
            # di-query buat ditampilkan di panel detail UI.
            conn.execute('''
                CREATE TABLE IF NOT EXISTS automation_runs (
                    run_id TEXT PRIMARY KEY,
                    automation_id TEXT NOT NULL,
                    mulai_at TIMESTAMP NOT NULL,
                    selesai_at TIMESTAMP,
                    status TEXT NOT NULL,
                    detail_json TEXT,
                    FOREIGN KEY (automation_id) REFERENCES automations(id) ON DELETE CASCADE
                )
            ''')
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_automation_id ON automation_runs(automation_id)"
            )
    except sqlite3.Error as e:
        logging.error(f"Gagal inisialisasi tabel automations/automation_runs: {e}")
        raise


def tambah_automation(
    nama_alur: str,
    tipe_jadwal: str,
    waktu_eksekusi: str,
    steps: List[Dict[str, Any]],
    depends_on_id: Optional[str] = None,
    depends_on_status: Optional[str] = None,
) -> str:
    """Menambah jadwal baru ke database dengan validasi data.

    `steps` sekarang list of dict (BUKAN list of string lagi), format per step:
        {
            "tool": "<nama_tool>",
            "args": {
                "<nama_param>": {"type": "manual", "value": <apapun>}
                                  ATAU
                                 {"type": "from_step", "step": <index step lain>},
                ...
            },
            "stop_if_output_contains": "<substring>" atau None
        }
    Fungsi ini TIDAK validasi isi tiap step secara detail (itu tanggung jawab
    UI/daemon pas baca) -- di sini cuma pastiin bentuknya list & gak kosong.

    `tipe_jadwal="EVENT"`: `waktu_eksekusi` gak dipakai buat jadwal (isi apa
    aja, mis. "-"), tapi WAJIB isi `depends_on_id` (id automation lain) dan
    `depends_on_status` ("SUKSES"|"GAGAL"|"DIHENTIKAN"|"ANY") -- automation
    ini baru dianggap "due" oleh daemon kalau automation dependensi barusan
    selesai dengan status yang cocok. Lihat automation_daemon.is_due().
    """
    if not nama_alur or not nama_alur.strip():
        raise ValueError("Nama alur tidak boleh kosong.")
    if not steps:
        raise ValueError("Langkah (steps) automation tidak boleh kosong.")
    if not isinstance(steps, list):
        raise ValueError("steps harus berupa list.")
    for s in steps:
        if not isinstance(s, dict):
            raise ValueError("Setiap step harus dict.")
        # [FIX] Validasi kelewat waktu nambah step type "ai_transform" -- dulu
        # SEMUA step dipaksa punya key 'tool', padahal ai_transform gak punya
        # itu (dia punya 'sumber_step'). Sekarang validasinya cabang per type.
        if s.get("type") == "ai_transform":
            if "sumber_step" not in s:
                raise ValueError("Step ai_transform harus punya key 'sumber_step'.")
        elif "tool" not in s:
            raise ValueError("Setiap step tool harus punya key 'tool'.")
    if tipe_jadwal not in ["DAILY", "INTERVAL", "EVENT"]:
        raise ValueError(f"Tipe jadwal '{tipe_jadwal}' tidak valid. Gunakan DAILY, INTERVAL, atau EVENT.")
    if tipe_jadwal == "EVENT":
        if not depends_on_id:
            raise ValueError("Tipe jadwal EVENT butuh depends_on_id (automation pemicu).")
        if depends_on_status not in ("SUKSES", "GAGAL", "DIHENTIKAN", "ANY"):
            raise ValueError("depends_on_status harus salah satu dari SUKSES/GAGAL/DIHENTIKAN/ANY.")
    elif not waktu_eksekusi:
        raise ValueError("Waktu eksekusi harus diisi.")

    alur_id = str(uuid.uuid4())
    steps_json = json.dumps(steps, ensure_ascii=False)

    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                "INSERT INTO automations "
                "(id, nama_alur, tipe_jadwal, waktu_eksekusi, steps_json, depends_on_id, depends_on_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (alur_id, nama_alur.strip(), tipe_jadwal, (waktu_eksekusi or "-").strip(), steps_json,
                 depends_on_id, depends_on_status)
            )
        return alur_id
    except sqlite3.Error as e:
        logging.error(f"Error saat menambah automation: {e}")
        raise


def update_automation(
    alur_id: str,
    nama_alur: str,
    tipe_jadwal: str,
    waktu_eksekusi: str,
    steps: List[Dict[str, Any]],
    depends_on_id: Optional[str] = None,
    depends_on_status: Optional[str] = None,
):
    """ Update automation yang SUDAH ADA -- validasi persis sama dengan
    tambah_automation() (lihat situ buat detail format `steps`). `status`
    (RUNNING/STOPPED) TIDAK disentuh di sini -- itu tetap lewat ubah_status().

    `last_run_at` SENGAJA di-reset ke NULL -- setelah step/jadwal berubah,
    riwayat "terakhir jalan" yang lama itu ngacu ke DEFINISI LAMA (bisa beda
    total isinya), jadi gak akurat lagi buat ngitung due-time versi baru.
    Daemon bakal nganggep ini "belum pernah jalan" dan due lagi wajar."""
    if not nama_alur or not nama_alur.strip():
        raise ValueError("Nama alur tidak boleh kosong.")
    if not steps:
        raise ValueError("Langkah (steps) automation tidak boleh kosong.")
    if not isinstance(steps, list):
        raise ValueError("steps harus berupa list.")
    for s in steps:
        if not isinstance(s, dict):
            raise ValueError("Setiap step harus dict.")
        if s.get("type") == "ai_transform":
            if "sumber_step" not in s:
                raise ValueError("Step ai_transform harus punya key 'sumber_step'.")
        elif "tool" not in s:
            raise ValueError("Setiap step tool harus punya key 'tool'.")
    if tipe_jadwal not in ["DAILY", "INTERVAL", "EVENT"]:
        raise ValueError(f"Tipe jadwal '{tipe_jadwal}' tidak valid. Gunakan DAILY, INTERVAL, atau EVENT.")

    if tipe_jadwal == "EVENT":
        if not depends_on_id:
            raise ValueError("tipe_jadwal EVENT butuh depends_on_id (automation pemicu).")
        if depends_on_status not in ("SUKSES", "GAGAL", "DIHENTIKAN", "ANY"):
            raise ValueError("depends_on_status harus salah satu dari: SUKSES, GAGAL, DIHENTIKAN, ANY.")
        waktu_eksekusi = waktu_eksekusi or "-"
    else:
        if not waktu_eksekusi:
            raise ValueError("Waktu eksekusi harus diisi.")
        depends_on_id = None
        depends_on_status = None

    steps_json = json.dumps(steps, ensure_ascii=False)
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            cursor = conn.execute(
                "UPDATE automations SET nama_alur = ?, tipe_jadwal = ?, waktu_eksekusi = ?, "
                "steps_json = ?, depends_on_id = ?, depends_on_status = ?, last_run_at = NULL "
                "WHERE id = ?",
                (nama_alur.strip(), tipe_jadwal, (waktu_eksekusi or "-").strip(), steps_json,
                 depends_on_id, depends_on_status, alur_id)
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Automation dengan id {alur_id} tidak ditemukan.")
    except sqlite3.Error as e:
        logging.error(f"Error saat update automation: {e}")
        raise


def ambil_semua_automation() -> List[Dict[str, Any]]:
    """Mengambil semua data untuk ditampilkan di UI dan dibaca oleh Daemon."""
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM automations ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error as e:
        logging.error(f"Error saat mengambil data automation: {e}")
        return []


def ambil_automation_running() -> List[Dict[str, Any]]:
    """ Khusus dipakai daemon -- cuma tarik yang statusnya RUNNING,
    biar daemon gak perlu filter ulang di Python tiap polling cycle."""
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM automations WHERE status = 'RUNNING'").fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error as e:
        logging.error(f"Error saat mengambil automation RUNNING: {e}")
        return []


def ambil_satu_automation(alur_id: str) -> Optional[Dict[str, Any]]:
    """ Ambil 1 automation by id -- dipakai endpoint /run_now daemon."""
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM automations WHERE id = ?", (alur_id,)).fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error(f"Error saat mengambil 1 automation: {e}")
        return None


def ubah_status(alur_id: str, status_baru: str):
    """Mengubah status menjadi RUNNING atau STOPPED."""
    if status_baru not in ["RUNNING", "STOPPED"]:
        raise ValueError("Status hanya boleh 'RUNNING' atau 'STOPPED'.")
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            cursor = conn.execute("UPDATE automations SET status = ? WHERE id = ?", (status_baru, alur_id))
            if cursor.rowcount == 0:
                logging.warning(f"Update gagal: Automation dengan ID {alur_id} tidak ditemukan.")
    except sqlite3.Error as e:
        logging.error(f"Error saat mengubah status automation: {e}")
        raise


def update_last_run(alur_id: str, waktu_epoch: Optional[float] = None):
    """ Dipanggil daemon SETELAH selesai eksekusi 1 automation (apapun
    hasilnya, sukses/gagal/dihentikan) -- update penanda 'terakhir jalan'."""
    waktu_epoch = waktu_epoch if waktu_epoch is not None else time.time()
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                "UPDATE automations SET last_run_at = ? WHERE id = ?",
                (waktu_epoch, alur_id)
            )
    except sqlite3.Error as e:
        logging.error(f"Error saat update last_run_at: {e}")
        raise


def catat_run(automation_id: str, mulai_at: float, selesai_at: float, status: str, detail: Any) -> str:
    """ Simpan 1 baris riwayat eksekusi ke automation_runs.
    `status`: "SUKSES" | "GAGAL" | "DIHENTIKAN" (goal sudah tercapai di
    tengah jalan, lihat stop_if_output_contains).
    `detail`: apapun yang bisa di-json.dumps (mis. list hasil per step)."""
    run_id = str(uuid.uuid4())
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                "INSERT INTO automation_runs (run_id, automation_id, mulai_at, selesai_at, status, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, automation_id, mulai_at, selesai_at, status, json.dumps(detail, default=str, ensure_ascii=False))
            )
        return run_id
    except sqlite3.Error as e:
        logging.error(f"Error saat mencatat automation_runs: {e}")
        raise


def ambil_riwayat(automation_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """ Ambil N riwayat eksekusi terakhir 1 automation, terbaru duluan --
    dipakai panel detail UI buat nampilin histori jalan/gagal."""
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM automation_runs WHERE automation_id = ? ORDER BY mulai_at DESC LIMIT ?",
                (automation_id, limit)
            ).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error as e:
        logging.error(f"Error saat mengambil riwayat: {e}")
        return []


def hapus_automation(alur_id: str):
    """Menghapus jadwal secara permanen (riwayat run ikut kehapus via CASCADE)."""
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys = ON;")  # WAJIB per-koneksi biar CASCADE aktif
            cursor = conn.execute("DELETE FROM automations WHERE id = ?", (alur_id,))
            if cursor.rowcount == 0:
                logging.warning(f"Delete gagal: Automation dengan ID {alur_id} tidak ditemukan.")
    except sqlite3.Error as e:
        logging.error(f"Error saat menghapus automation: {e}")
        raise