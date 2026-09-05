import json
import sqlite3
from dataclasses import asdict, is_dataclass

# Mengambil path secara terpusat dari config
from core_agent.config import sqlite_db_path

KOLOM_TAMBAHAN = [
    ("dikelola_oleh", "TEXT"),    # nama perekrut/HR dari ManagedByContainer
    ("foto_pengelola", "TEXT"),   # url foto profil perekrut
    ("tag_pengelola", "TEXT"),    # badge di sebelah nama, mis. "Perusahaan Premium"
]

def _migrasi_kolom_tambahan(cursor):
    kolom_ada = {row[1] for row in cursor.execute("PRAGMA table_info(lowongan_kerja)").fetchall()}
    for nama_kolom, tipe_sql in KOLOM_TAMBAHAN:
        if nama_kolom not in kolom_ada:
            cursor.execute(f"ALTER TABLE lowongan_kerja ADD COLUMN {nama_kolom} {tipe_sql}")
            print(f"[job_db] Migrasi: kolom '{nama_kolom}' ditambahkan ke lowongan_kerja.")

def init_job_db():
    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        cursor = conn.cursor()

        # AKTIFKAN MODE WAL (Write-Ahead Logging) AGAR MULTI-PROCESS AMAN
        cursor.execute("PRAGMA journal_mode=WAL;")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS lowongan_kerja (
                job_id              TEXT PRIMARY KEY,
                url                 TEXT NOT NULL,
                judul_pekerjaan     TEXT,
                perusahaan          TEXT,
                url_perusahaan      TEXT,
                gaji                TEXT,
                kategori            TEXT,
                tipe_kerja          TEXT,
                lokasi_kerja        TEXT,
                pendidikan_min      TEXT,
                pengalaman_min      TEXT,
                skills              TEXT,   -- JSON list
                deskripsi_pekerjaan TEXT,
                industri_perusahaan TEXT,
                ukuran_perusahaan   TEXT,
                website_perusahaan  TEXT,
                deskripsi_perusahaan TEXT,
                alamat_kantor       TEXT,
                tayang_pertama      TEXT,
                diperbarui          TEXT,
                tanggal_scrape      TEXT
            )
        ''')

        # Tabel lama (sebelum kolom ManagedBy ditambahkan) otomatis di-upgrade
        # di sini -- tidak menyentuh baris data yang sudah ada.
        _migrasi_kolom_tambahan(cursor)

        conn.commit()

def cari_dikelola_oleh(nama_perusahaan: str) -> str:
    """Cari nilai kolom `dikelola_oleh` (nama pengelola postingan dari
    halaman Glints, "Loker ini dikelola oleh") dari tabel lowongan_kerja
    berdasarkan nama perusahaan (LIKE match, ambil yang paling baru
    discrape). Return string kosong kalau tidak ketemu/kolom kosong.
 
    CATATAN: kolom ini kadang isinya nama PERUSAHAAN itu sendiri (bukan
    nama orang), tergantung tipe akun yang posting loker di Glints --
    filter/validasi apakah ini beneran nama orang dilakukan di
    contact_osint_func.ambil_nama_pengelola(), bukan di sini (di sini
    murni ambil data mentahnya saja)."""
    init_job_db()
    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        row = conn.execute(
            """
            SELECT dikelola_oleh FROM lowongan_kerja
            WHERE perusahaan LIKE ? AND dikelola_oleh != ''
            ORDER BY tanggal_scrape DESC LIMIT 1
            """,
            (f"%{nama_perusahaan}%",),
        ).fetchone()
    return row[0] if row and row[0] else ""

def simpan_lowongan(data: list) -> int:
    """Upsert list LowonganDetail (dataclass atau dict sepadan) ke tabel
    lowongan_kerja. Kunci upsert: job_id. Return jumlah row yang ditulis."""
    if not data:
        return 0

    init_job_db()

    baris_dict = []
    for item in data:
        row = asdict(item) if is_dataclass(item) else dict(item)
        row["skills"] = json.dumps(row.get("skills", []), ensure_ascii=False)
        baris_dict.append(row)

    kolom = list(baris_dict[0].keys())
    placeholder = ", ".join(["?"] * len(kolom))
    update_clause = ", ".join(f"{k}=excluded.{k}" for k in kolom if k != "job_id")

    sql = f"""
        INSERT INTO lowongan_kerja ({", ".join(kolom)})
        VALUES ({placeholder})
        ON CONFLICT(job_id) DO UPDATE SET {update_clause}
    """
    rows = [tuple(row[k] for k in kolom) for row in baris_dict]

    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executemany(sql, rows)
        conn.commit()

    return len(rows)

def cari_lowongan(keyword: str = "", limit: int = 10) -> list:
    """Query lowongan tersimpan, opsional filter keyword pada judul/nama
    perusahaan, urut dari yang paling baru discrape. Return list of dict."""
    init_job_db()

    query = """
        SELECT judul_pekerjaan, perusahaan, gaji, tipe_kerja, lokasi_kerja,
               pendidikan_min, pengalaman_min, url
        FROM lowongan_kerja
    """
    params: tuple = ()
    if keyword:
        query += " WHERE judul_pekerjaan LIKE ? OR perusahaan LIKE ?"
        params = (f"%{keyword}%", f"%{keyword}%")
    query += " ORDER BY tanggal_scrape DESC LIMIT ?"
    params = params + (limit,)

    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()

    return [dict(r) for r in rows]

def hitung_lowongan_tersimpan() -> int:
    """Jumlah total lowongan yang tersimpan di database."""
    init_job_db()
    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        return conn.execute("SELECT COUNT(*) FROM lowongan_kerja").fetchone()[0]

def cari_website_perusahaan(nama_perusahaan: str) -> str:
    """Cari website_perusahaan dari tabel lowongan_kerja berdasarkan nama
    perusahaan (LIKE match, ambil yang paling baru discrape). Dipakai
    contact_osint_func.py untuk resolve domain email tanpa perlu scrape
    ulang. Return string kosong kalau tidak ketemu."""
    init_job_db()
    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        row = conn.execute(
            """
            SELECT website_perusahaan FROM lowongan_kerja
            WHERE perusahaan LIKE ? AND website_perusahaan != ''
            ORDER BY tanggal_scrape DESC LIMIT 1
            """,
            (f"%{nama_perusahaan}%",),
        ).fetchone()
    return row[0] if row and row[0] else ""