import sqlite3
from datetime import datetime, timezone

from core_agent.config import sqlite_db_path


def init_email_history_db():
    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS riwayat_lamaran (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                pengirim_email  TEXT,
                penerima_email  TEXT,
                nama_perusahaan TEXT,
                url_lowongan    TEXT,
                subjek          TEXT,
                status_kirim    TEXT,
                pesan_error     TEXT,
                waktu_kirim     TEXT
            )
        ''')
        conn.commit()


def simpan_riwayat_kirim(data: dict) -> int:
    """Insert 1 baris riwayat pengiriman (SELALU insert baru, bukan
    upsert). Return id row yang baru dibuat."""
    init_email_history_db()

    baris = dict(data)
    baris.setdefault("waktu_kirim", datetime.now(timezone.utc).isoformat())

    kolom = ["pengirim_email", "penerima_email", "nama_perusahaan",
             "url_lowongan", "subjek", "status_kirim", "pesan_error", "waktu_kirim"]

    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.execute(
            f"INSERT INTO riwayat_lamaran ({', '.join(kolom)}) "
            f"VALUES ({', '.join(['?'] * len(kolom))})",
            tuple(baris.get(k, "") for k in kolom),
        )
        conn.commit()
        return cursor.lastrowid


def cari_riwayat_kirim(perusahaan: str = "", penerima: str = "", limit: int = 20) -> list:
    """Query riwayat pengiriman, opsional filter nama perusahaan &/atau
    alamat penerima, urut dari yang paling baru."""
    init_email_history_db()

    query = """
        SELECT id, pengirim_email, penerima_email, nama_perusahaan,
               url_lowongan, subjek, status_kirim, pesan_error, waktu_kirim
        FROM riwayat_lamaran
        WHERE 1=1
    """
    params: list = []
    if perusahaan:
        query += " AND nama_perusahaan LIKE ?"
        params.append(f"%{perusahaan}%")
    if penerima:
        query += " AND penerima_email LIKE ?"
        params.append(f"%{penerima}%")
    query += " ORDER BY waktu_kirim DESC LIMIT ?"
    params.append(limit)

    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def sudah_pernah_kirim(penerima_email: str, nama_perusahaan: str = "") -> dict:
    """Cek apakah SUDAH PERNAH berhasil kirim ke penerima ini (opsional
    difilter nama perusahaan juga). Return dict riwayat terakhir yang
    BERHASIL kalau ada, atau None kalau belum pernah."""
    init_email_history_db()

    query = """
        SELECT id, waktu_kirim, subjek, status_kirim FROM riwayat_lamaran
        WHERE penerima_email = ? AND status_kirim = 'berhasil'
    """
    params: list = [penerima_email]
    if nama_perusahaan:
        query += " AND nama_perusahaan LIKE ?"
        params.append(f"%{nama_perusahaan}%")
    query += " ORDER BY waktu_kirim DESC LIMIT 1"

    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(query, params).fetchone()
    return dict(row) if row else None