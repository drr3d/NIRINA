import sqlite3
import json

from tools.dict_factory import dict_factory
from tools.freeform_calculation import calculate_age_from_entry_year
from core_agent.config import sqlite_db_path
from core_agent.registry import ToolRegistry

try:
    # Coba impor fungsi versi baru (Hybrid Search)
    from core_agent.agent_tools import get_hybrid_retriever
    HAS_HYBRID = True
except ImportError:
    # Jika tidak ada, fallback ke versi lama (Chroma Vector saja)
    from core_agent.agent_tools import retriever
    HAS_HYBRID = False

@ToolRegistry.register(is_sensitive=False)
def evaluasi_kandidat(nama_kandidat: str, posisi_lowongan: str) -> str:
    """
    GUNAKAN ALAT INI UNTUK MENGEVALUASI CV KANDIDAT TERHADAP LOWONGAN.
    Hasilnya akan berupa: MATCH (lanjut), CAUTION (butuh review HR), atau REJECT.
    """
    from core_agent.agent_nodes import LLMs

    # 1. Ambil Data Kandidat
    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        conn.row_factory = dict_factory

        # 1. PASANG PRAGMA WAL DI SINI (Baris pertama operasi)
        conn.execute("PRAGMA journal_mode=WAL;")

        kandidat = conn.execute("SELECT * FROM kandidat WHERE LOWER(nama_kandidat) LIKE ?", (f"%{nama_kandidat.lower().strip()}%",)).fetchone()
    
    # 2. Ambil Data Lowongan
    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        conn.row_factory = dict_factory

        # 1. PASANG PRAGMA WAL DI SINI (Baris pertama operasi)
        conn.execute("PRAGMA journal_mode=WAL;")

        lowongan = conn.execute("SELECT * FROM lowongan WHERE posisi = ?", (posisi_lowongan,)).fetchone()

    if not kandidat or not lowongan:
        return "Data kandidat atau lowongan tidak ditemukan."

    # 3. Analisis AI (Menggunakan LLM untuk mencocokkan)
    prompt_evaluasi = f"""
    Kamu adalah expert HR. Analisis apakah kandidat ini cocok untuk lowongan {posisi_lowongan}.
    Kandidat: {kandidat['nama_kandidat']}, Skill: {kandidat['skill_utama']}.
    Lowongan: {lowongan['posisi']}, Kebutuhan: {lowongan['keyword_wajib']}.
    
    Berikan status: MATCH, CAUTION, atau REJECT.
    Berikan alasan singkat dalam bahasa Indonesia.
    """
    
    response = LLMs.invoke(prompt_evaluasi).content
    return f"Hasil Evaluasi {nama_kandidat} untuk {posisi_lowongan}:\n{response}"
    
@ToolRegistry.register(is_sensitive=False)
def tool_hitung_usia_kandidat(tahun_masuk_kuliah: int) -> str:
    """
    GUNAKAN ALAT INI jika kamu perlu mengetahui estimasi umur/usia seorang kandidat.
    Kamu harus memberikan parameter 'tahun_masuk_kuliah' (berupa angka integer, misal: 2008).
    Dapatkan tahun masuk kuliah dari alat 'lihat_profil_terstruktur_kandidat' terlebih dahulu.
    """
    try:
        usia = calculate_age_from_entry_year(tahun_masuk_kuliah)
        return f"Berdasarkan tahun masuk kuliah {tahun_masuk_kuliah}, estimasi usia kandidat adalah {usia} tahun."
    except Exception as e:
        return f"Gagal menghitung usia: {e}"
    
# ==========================================
# --- 3 TOOL BARU PENGGANTI AKSES DATABASE ---
# ==========================================
@ToolRegistry.register(is_sensitive=False)
def lihat_daftar_kandidat() -> str:
    """
    GUNAKAN ALAT INI PERTAMA KALI JIKA USER BERTANYA TENTANG DAFTAR KANDIDAT.
    """
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            conn.row_factory = dict_factory
            cursor = conn.cursor()

            # AKTIFKAN MODE WAL (Write-Ahead Logging) AGAR MULTI-PROCESS AMAN
            cursor.execute("PRAGMA journal_mode=WAL;")

            cursor.execute("SELECT nama_kandidat, pendidikan_terakhir, lama_bekerja_tahun, skill_utama FROM kandidat")
            data = cursor.fetchall()
            
        if not data:
            return "Database HR belum memiliki data kandidat."
            
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error membaca database SQLite: {e}"