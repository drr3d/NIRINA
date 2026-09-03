import sqlite3

from core_agent.config import sqlite_db_path

def cari_lowongan_belum_dilamar(keyword: str = "", limit: int = 10) -> list:
    """Cari lowongan tersimpan yang cocok `keyword` (judul/perusahaan)
    dan BELUM PERNAH berhasil dikirimi lamaran (dicek lewat JOIN ke
    riwayat_lamaran berdasarkan url), urut dari yang PALING BARU
    discrape. Return list of dict."""
    query = """
        SELECT lk.job_id, lk.judul_pekerjaan, lk.perusahaan, lk.url,
               lk.gaji, lk.tipe_kerja, lk.lokasi_kerja, lk.tanggal_scrape
        FROM lowongan_kerja lk
        WHERE 1=1
    """
    params: list = []
    if keyword:
        query += " AND (lk.judul_pekerjaan LIKE ? OR lk.perusahaan LIKE ?)"
        params.extend([f"%{keyword}%", f"%{keyword}%"])

    query += """
        AND NOT EXISTS (
            SELECT 1 FROM riwayat_lamaran rl
            WHERE rl.url_lowongan = lk.url
              AND rl.status_kirim = 'berhasil'
        )
        ORDER BY lk.tanggal_scrape DESC
        LIMIT ?
    """
    params.append(limit)

    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]

def cek_status_lamaran_per_lowongan(url_lowongan: str) -> dict:
    """Cek status lamaran buat SATU lowongan spesifik (by url) -- kapan
    terakhir (kalau pernah) dikirim & ke siapa. Return dict kosong kalau
    belum pernah dikirim sama sekali."""
    query = """
        SELECT penerima_email, status_kirim, waktu_kirim
        FROM riwayat_lamaran
        WHERE url_lowongan = ?
        ORDER BY waktu_kirim DESC
        LIMIT 1
    """
    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(query, (url_lowongan,)).fetchone()
    return dict(row) if row else {}