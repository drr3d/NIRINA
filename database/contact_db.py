import sqlite3
from datetime import datetime, timezone

from core_agent.config import sqlite_db_path

def init_contact_db():
    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS kontak_perusahaan (
                email               TEXT PRIMARY KEY,
                perusahaan          TEXT,
                domain_email        TEXT,
                nama_orang          TEXT,
                jabatan             TEXT,
                pola_tebakan        TEXT,
                status_mx           TEXT,
                status_smtp         TEXT,
                sumber              TEXT,
                tanggal_ditemukan   TEXT
            )
        ''')
        conn.commit()

def simpan_kontak(data: list) -> int:
    """Upsert list dict kontak ke tabel kontak_perusahaan. Kunci upsert:
    email. Return jumlah row yang ditulis."""
    if not data:
        return 0

    init_contact_db()

    kolom = ["email", "perusahaan", "domain_email", "nama_orang", "jabatan",
             "pola_tebakan", "status_mx", "status_smtp", "sumber", "tanggal_ditemukan"]
    placeholder = ", ".join(["?"] * len(kolom))
    update_clause = ", ".join(f"{k}=excluded.{k}" for k in kolom if k != "email")

    sql = f"""
        INSERT INTO kontak_perusahaan ({", ".join(kolom)})
        VALUES ({placeholder})
        ON CONFLICT(email) DO UPDATE SET {update_clause}
    """

    rows = []
    for item in data:
        row = dict(item)
        row.setdefault("tanggal_ditemukan", datetime.now(timezone.utc).isoformat())
        rows.append(tuple(row.get(k, "") for k in kolom))

    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executemany(sql, rows)
        conn.commit()

    return len(rows)


def cari_kontak(perusahaan: str = "", limit: int = 20) -> list:
    """Query kandidat kontak tersimpan, opsional filter nama perusahaan,
    urut dari yang paling baru ditemukan. Return list of dict."""
    init_contact_db()

    query = """
        SELECT email, perusahaan, domain_email, nama_orang, jabatan,
               pola_tebakan, status_mx, status_smtp, sumber, tanggal_ditemukan
        FROM kontak_perusahaan
    """
    params: tuple = ()
    if perusahaan:
        query += " WHERE perusahaan LIKE ?"
        params = (f"%{perusahaan}%",)
    query += " ORDER BY tanggal_ditemukan DESC LIMIT ?"
    params = params + (limit,)

    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()

    return [dict(r) for r in rows]