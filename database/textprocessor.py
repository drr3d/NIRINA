import os
import numpy as np
import json, re, pickle
from pathlib import Path
from datetime import datetime
import sqlite3

import sys
from pathlib import Path

from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

from langchain_ollama import OllamaEmbeddings, ChatOllama
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate

# 1. Dapatkan path dari root folder proyek (naik 1 tingkat dari folder 'tools')
root_dir = Path(__file__).resolve().parent.parent

# 2. Masukkan root folder ke sistem path Python jika belum terdaftar
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

# Sesuaikan dengan path database Anda
from core_agent.config import db_path, config_path, sqlite_db_path

# =====================================================================
# [OPTIMASI 1, 2, & 4]: MATRYOSHKA EMBEDDINGS DITARUH DI HULU (SINI)
# =====================================================================
class OptimizedCPUEmbeddings(HuggingFaceEmbeddings):
    target_dimensions: int = 256 # Pangkas dimensi agar enteng di RAM/CPU

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        prefixed_texts = [f"search_document: {t}" for t in texts]
        embs = super().embed_documents(prefixed_texts)
        return self._truncate_and_normalize(embs)
        
    def embed_query(self, text: str) -> list[float]:
        prefixed_text = f"search_query: {text}"
        emb = super().embed_query(prefixed_text)
        return self._truncate_and_normalize([emb])[0]
        
    def _truncate_and_normalize(self, embeddings: list[list[float]]) -> list[list[float]]:
        arr = np.array(embeddings)[:, :self.target_dimensions]
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        normalized = np.divide(arr, norms, out=np.zeros_like(arr), where=norms!=0)
        return normalized.tolist()

embeddings = None
embeddings_method = 2
if embeddings_method == 0:
    embeddings = OllamaEmbeddings(model="nomic-embed-text")
elif embeddings_method == 1:
    embeddings = HuggingFaceEmbeddings(
        model_name="BAAI/bge-m3",
        encode_kwargs={'normalize_embeddings': True}
    )
elif embeddings_method == 2:
    # Gunakan Class Optimized yang baru dibuat
    embeddings = OptimizedCPUEmbeddings(
        model_name="BAAI/bge-m3",
        model_kwargs={'device': 'cpu'},
        encode_kwargs={'normalize_embeddings': False} # Normalisasi sudah dihandle class
    )

vector_db = Chroma(persist_directory=str(db_path), 
                   embedding_function=embeddings,
                   collection_metadata={"hnsw:space": "cosine"})

# --- 2. PROMPT EXTRACTION ---
# Kita gunakan "riwayat_periode_kerja" agar AI tidak bingung.
# [NEW UPGRADE]: Menambahkan flag 'pekerjaan_aktif_saat_ini' dan 'status_kalkulasi' untuk transparansi HR.
# [PERBAIKAN]: Komentar double-slash (//) DIHAPUS dari dalam struktur JSON agar tidak membuat Qwen error.
extraction_prompt = ChatPromptTemplate.from_messages([
    ("system", 
     "Kamu adalah Senior HR Data Extractor. Tugasmu mengekstrak informasi dari teks CV "
     "(CV bisa berbahasa Indonesia atau English) ke dalam format JSON yang presisi.\n\n"
     "ATURAN MUTLAK:\n"
     "1. Keluarkan HANYA output JSON yang valid. Tidak boleh ada teks pembuka, penjelasan, atau penutup.\n"
     "2. JANGAN gunakan markdown code blocks (seperti ```json). Langsung mulai dengan karakter {{\n"
     "   dan akhiri dengan }}.\n"
     "3. Ekstrak 'pekerjaan_aktif_saat_ini' berisi NAMA PERUSAHAAN yang periode kerjanya masih 'Now', 'Current', atau 'Present'. Kosongkan array jika tidak ada.\n"
     "4. Isi 'status_kalkulasi' dengan 'Kotor' jika ada pekerjaan yang masih aktif. Isi 'Bersih' jika semua pekerjaan memiliki tahun selesai yang pasti.\n"
     "5. WAJIB ikuti format JSON berikut tanpa mengubah key:\n"
     "{{\n"
     "  \"nama_kandidat\": \"Nama Lengkap\",\n"
     "  \"nik\": \"Nomor NIK/KTP (Ambil semua angkanya, abaikan titik/strip)\",\n"
     "  \"email\": \"contoh@email.com\",\n"
     "  \"no_hp\": \"08xxxxxxxx\",\n"
     "  \"gender\": \"Pria/Wanita/Tidak Diketahui\",\n"
     "  \"usia\": 0,\n"
     "  \"asal_daerah\": \"Nama Kota Domisili\",\n"
     "  \"pekerjaan_aktif_saat_ini\": [\"Nama Perusahaan 1\"],\n"
     "  \"status_kalkulasi\": \"Kotor/Bersih\",\n"
     "  \"riwayat_periode_kerja\": [\"August 2013 to Current\", \"October 2010 to March 2013\"],\n"
     "  \"pendidikan_terakhir\": \"SMA/D3/S1/S2/S3\",\n"
     "  \"jurusan\": \"Nama Jurusan\",\n"
     "  \"ipk\": 0.0,\n"
     "  \"skill_utama\": [\"Skill1\", \"Skill2\"]\n"
     "}}\n"
     "PENTING: Jangan mengarang informasi. Jika data tidak ditemukan, gunakan string kosong \"\" untuk teks, angka 0 untuk nominal, atau array kosong []."
    ),
    ("human", "Teks CV:\n{text_cv}")
])

def auto_screening_cv(profil_kandidat: dict, model_name: str) -> tuple[str, str]:
    """
    Fungsi otonom untuk mengevaluasi kandidat terhadap lowongan aktif 
    langsung setelah proses ekstraksi CV selesai.
    """
    try:
        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
            cursor = conn.cursor()

            # AKTIFKAN MODE WAL (Write-Ahead Logging) AGAR MULTI-PROCESS AMAN
            cursor.execute("PRAGMA journal_mode=WAL;")

            # 1. Pastikan tabel lowongan ada agar tidak crash saat pertama kali jalan
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS lowongan (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    posisi TEXT,
                    deskripsi TEXT,
                    keyword_wajib TEXT,
                    is_aktif INTEGER DEFAULT 1
                )
            ''')
            # 2. Ambil 1 lowongan yang statusnya masih aktif (paling baru ditambahkan)
            cursor.execute("SELECT posisi, keyword_wajib FROM lowongan WHERE is_aktif = 1 ORDER BY id DESC LIMIT 1")
            lowongan = cursor.fetchone()
            
        # Jika belum ada lowongan yang dibuat HR, kembalikan status netral
        if not lowongan:
            return "UNSCREENED", "Menunggu evaluasi. Belum ada data lowongan aktif di sistem saat CV ini masuk."
            
        posisi_lowongan, keyword_lowongan = lowongan
        
        # 3. Merakit Prompt Evaluasi Otomatis (Format Wajib JSON)
        prompt_screening = ChatPromptTemplate.from_messages([
            ("system", 
             "Kamu adalah AI Senior HR Screener otomatis yang bekerja di balik layar.\n"
             "Tugasmu: Evaluasi kecocokan data JSON kandidat terhadap kriteria lowongan aktif.\n\n"
             "ATURAN MUTLAK KELUARAN:\n"
             "1. Keluarkan HANYA format JSON valid. Tidak ada teks pengantar.\n"
             "2. Gunakan key 'status_screening' (Isi eksklusif dengan: MATCH, CAUTION, atau REJECT).\n"
             "3. Gunakan key 'alasan_screening' (Berikan 2-3 kalimat penjelasan analitis, bandingkan skill CV dengan keyword lowongan).\n\n"
             "KRITERIA PENILAIAN:\n"
             "- MATCH: Skill teknis & pengalaman >= 80% sesuai keyword lowongan.\n"
             "- CAUTION: Relevansi 50-79%, ATAU kandidat memiliki gap/latar belakang unik yang butuh validasi human (HR).\n"
             "- REJECT: Relevansi < 50%, ATAU tidak memenuhi kualifikasi dasar sama sekali."
            ),
            ("human", 
             # PERBAIKAN 1: Hapus f-string, gunakan placeholder standar Langchain
             "Posisi Lowongan: {posisi}\n"
             "Keyword Wajib: {keyword}\n\n"
             "Data Ekstraksi CV Kandidat:\n{data_cv}"
            )
        ])
        
        print(f"-> [Auto-Screening] Mencocokkan dengan lowongan aktif: {posisi_lowongan}...")
        
        # Panggil LLM dengan format JSON agar tidak melantur
        llm_screener = ChatOllama(model=model_name, temperature=0.0, format="json")
        
        # PERBAIKAN 2: Injeksi data lewat invoke(), bukan langsung ditempel di string
        raw_result = (prompt_screening | llm_screener).invoke({
            "posisi": posisi_lowongan,
            "keyword": keyword_lowongan,
            "data_cv": json.dumps(profil_kandidat, indent=2)
        }).content
        
        # Bersihkan format jika LLM menambahkan markdown ```json
        cleaned_output = re.sub(r'```json\s*|```', '', raw_result, flags=re.IGNORECASE).strip()
        hasil_json = json.loads(cleaned_output)
        
        status = hasil_json.get("status_screening", "CAUTION").upper()
        alasan = hasil_json.get("alasan_screening", "Terjadi kesalahan ekstraksi alasan dari respon AI.")
        
        # Beri tag posisi pada alasan agar HR tahu CV ini dievaluasi untuk posisi apa
        alasan_lengkap = f"[Evaluasi: {posisi_lowongan}] {alasan}"
        return status, alasan_lengkap
        
    except Exception as e:
        print(f"-> [Auto-Screening] Error saat proses screening: {e}")
        return "ERROR", f"Sistem gagal melakukan screening otomatis: {str(e)}"

def hitung_total_tahun_kerja(periode_list):
    """Fungsi ajaib pengubah teks tanggal menjadi angka (tahun) presisi"""
    if not isinstance(periode_list, list) or not periode_list:
        return 0.0
        
    # [PERBAIKAN]: Mengambil tahun dan bulan secara dinamis dari sistem (bukan hardcode 2026)
    now = datetime.now()
    tahun_sekarang = now.year
    bulan_sekarang = now.month
    
    bulan_map = {
        'jan':1, 'feb':2, 'mar':3, 'apr':4, 'may':5, 'jun':6, 
        'jul':7, 'aug':8, 'sep':9, 'oct':10, 'nov':11, 'dec':12
    }
    
    def parse_tgl(tgl_str):
        tgl_str = tgl_str.strip().lower()
        if any(word in tgl_str for word in ['current', 'present', 'now', 'sekarang']):
            return tahun_sekarang, bulan_sekarang
            
        thn_match = re.search(r'\b(19|20)\d{2}\b', tgl_str)
        thn = int(thn_match.group(0)) if thn_match else tahun_sekarang
        
        bln = 1
        for b_name, b_num in bulan_map.items():
            if b_name in tgl_str:
                bln = b_num
                break
        return thn, bln

    # [NEW UPGRADE]: ALGORITMA PENGGABUNGAN RENTANG (INTERVAL MERGING)
    # Tujuannya agar jika pelamar kerja di 2 perusahaan pada bulan yang sama (tumpang tindih),
    # bulan tersebut hanya dihitung 1 kali, tidak di-double count.
    intervals = []
    
    for periode in periode_list:
        try:
            parts = re.split(r'\bto\b|\buntil\b|-|–', periode)
            if len(parts) >= 2:
                thn_mulai, bln_mulai = parse_tgl(parts[0])
                thn_selesai, bln_selesai = parse_tgl(parts[-1]) # Ambil part terakhir
                
                # Ubah ke bulan absolut (contoh: tahun 2010 bulan 2 = 2010*12 + 2)
                start_abs = (thn_mulai * 12) + bln_mulai
                end_abs = (thn_selesai * 12) + bln_selesai
                
                # Masukkan ke daftar interval jika start lebih kecil dari end
                if start_abs < end_abs:
                    intervals.append([start_abs, end_abs])
        except Exception:
            continue
            
    if not intervals:
        return 0.0

    # 1. Urutkan semua periode kerja berdasarkan waktu mulai (tanggal paling jadul ke paling baru)
    intervals.sort(key=lambda x: x[0])
    
    # 2. Proses penggabungan rentang (Merging)
    merged_intervals = [intervals[0]]
    
    for current in intervals[1:]:
        last_merged = merged_intervals[-1]
        
        # Jika kerjaan baru mulai sebelum kerjaan lama selesai (ada TUMPANG TINDIH)
        if current[0] <= last_merged[1]:
            # Gabungkan rentang waktunya ke waktu terjauh
            last_merged[1] = max(last_merged[1], current[1])
        else:
            # Jika tidak ada tumpang tindih, masukkan sebagai periode kerja baru (independen)
            merged_intervals.append(current)
            
    # 3. Hitung total bulan dari interval yang sudah dibersihkan dari tumpang tindih
    total_bulan = sum(interval[1] - interval[0] for interval in merged_intervals)
            
    return round(total_bulan / 12, 1)

def update_database_catalog(filename: str, text_cv: str, model_name: str, recreate_db: bool=False):
    """Fungsi untuk mengekstrak, mengevaluasi, dan menyimpan data terstruktur ke SQLite"""
    print(f"-> [AI Extractor] Menganalisis profil dari {filename} menggunakan model: {model_name}...")
    try:
        llm_extractor = ChatOllama(model=model_name, temperature=0.0, format="json")
        raw_output = (extraction_prompt | llm_extractor).invoke({"text_cv": text_cv}).content
        cleaned_output = re.sub(r'```json\s*|Markup|```', '', raw_output, flags=re.IGNORECASE).strip()
        
        match = re.search(r'\{.*\}', cleaned_output, re.DOTALL)
        if match:
            cleaned_output = match.group(0)
            
        profil_baru = json.loads(cleaned_output)
        
        # --- [PROSES BARU]: JALANKAN SCREENING OTOMATIS ---
        status_screening, alasan_screening = auto_screening_cv(profil_baru, model_name)
        
        # Kalkulasi tahun kerja
        list_periode = profil_baru.get("riwayat_periode_kerja", [])
        lama_bekerja = hitung_total_tahun_kerja(list_periode)
        
        nama = profil_baru.get("nama_kandidat", "Tidak Diketahui")
        # --- SANITASI NIK DENGAN REGEX ---
        nik_raw = str(profil_baru.get("nik", ""))
        # Hanya sisakan digit/angka (Hapus titik, strip, spasi, huruf typo, dll)
        nik_bersih = re.sub(r'\D', '', nik_raw)
        
        # Opsi Validasi: Jika ingin ketat, kosongkan jika tidak pas 16 digit. 
        # Jika ingin mentolerir typo pelamar (misal cuma 15 digit), biarkan saja tersimpan apa adanya.
        nik = nik_bersih if len(nik_bersih) >= 14 else "" 
        # ----------------------------------
        email = profil_baru.get("email", "")
        no_hp = profil_baru.get("no_hp", "")
        pekerjaan_aktif = json.dumps(profil_baru.get("pekerjaan_aktif_saat_ini", []))
        skill_utama = json.dumps(profil_baru.get("skill_utama", []))
        tgl_masuk = datetime.now().strftime("%Y-%m-%d")
        
        # Simpan ke SQLite menggunakan UPSERT (Insert or Replace)
        with sqlite3.connect(sqlite_db_path , timeout=30.0) as conn:
            cursor = conn.cursor()

            # === SEMENTARA: Hapus tabel kandidat lama agar skema baru terbentuk bersih ===
            # (Hapus baris ini setelah program dijalankan sekali agar tidak menghapus data terus-menerus)
            if recreate_db:
                cursor.execute("DROP TABLE IF EXISTS kandidat")
            
            # AKTIFKAN MODE WAL (Write-Ahead Logging) AGAR MULTI-PROCESS AMAN
            cursor.execute("PRAGMA journal_mode=WAL;")

            # [SAFE MIGRATION]: Tambahkan kolom baru jika ini adalah database lama yang belum di-reset
            try:
                cursor.execute("ALTER TABLE kandidat ADD COLUMN status_screening TEXT")
                cursor.execute("ALTER TABLE kandidat ADD COLUMN alasan_screening TEXT")
                cursor.execute("ALTER TABLE kandidat ADD COLUMN nik TEXT")
                cursor.execute("ALTER TABLE kandidat ADD COLUMN email TEXT")
                cursor.execute("ALTER TABLE kandidat ADD COLUMN no_hp TEXT")
            except sqlite3.OperationalError:
                pass # Mengabaikan error jika kolom sudah ada
                
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS kandidat (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nama_kandidat TEXT, nik TEXT, email TEXT, no_hp TEXT, gender TEXT, usia INTEGER, asal_daerah TEXT,
                    pekerjaan_aktif_saat_ini TEXT, status_kalkulasi TEXT, pendidikan_terakhir TEXT,
                    jurusan TEXT, ipk REAL, skill_utama TEXT, lama_bekerja_tahun REAL,
                    file_cv TEXT UNIQUE, tanggal_masuk TEXT, source INTEGER,
                    status_screening TEXT, alasan_screening TEXT
                )
            ''')
            
            cursor.execute('''
                INSERT OR REPLACE INTO kandidat (
                    nama_kandidat, nik, email, no_hp, gender, usia, asal_daerah, pekerjaan_aktif_saat_ini,
                    status_kalkulasi, pendidikan_terakhir, jurusan, ipk, skill_utama,
                    lama_bekerja_tahun, file_cv, tanggal_masuk, source,
                    status_screening, alasan_screening
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                nama, nik, email, no_hp, profil_baru.get("gender", ""), profil_baru.get("usia", 0),
                profil_baru.get("asal_daerah", ""), pekerjaan_aktif,
                profil_baru.get("status_kalkulasi", "Bersih"),
                profil_baru.get("pendidikan_terakhir", ""), profil_baru.get("jurusan", ""),
                profil_baru.get("ipk", 0.0), skill_utama, lama_bekerja, filename, tgl_masuk, 0,
                status_screening, alasan_screening
            ))
            conn.commit()

        status_calc = profil_baru.get('status_kalkulasi', 'Bersih')
        print(f"-> [AI Extractor] Sukses! Profil {nama} tersimpan ke SQLite.")
        print(f"   [Summary] Pengalaman: {lama_bekerja} Thn | Keputusan Screening: {status_screening}")
        
    except Exception as e:
        print(f"-> [AI Extractor] Gagal mengekstrak/menyimpan profil ke database: {e}")

def process_cv(file_path):
    # Mengambil nama file dari path lengkap (contoh: dari "C:/folder/user.pdf" jadi "user.pdf")
    filename = os.path.basename(file_path)
    print(f"\n=== Memproses file: {filename} ===")
    
    # =====================================================================
    # FASE 1: MEMBACA DAN MEMVALIDASI FILE PDF
    # =====================================================================
    try:
        # Membaca isi PDF menggunakan alat pembaca dari Langchain
        loader = PyPDFLoader(file_path)
        documents = loader.load()
        
        # Menghitung ada berapa lembar halaman di dalam CV tersebut
        jumlah_halaman = len(documents)
        
        # Jika CV lebih dari 5 halaman, tolak! Ini agar sistem tidak hang/lemot 
        # membaca dokumen pelamar yang terlalu panjang.
        if jumlah_halaman > 5:
            print(f"❌ [DITOLAK] File {filename} memiliki {jumlah_halaman} halaman (Maksimal 5).")
            return False
            
        print(f"-> [Validasi Lolos] CV terdiri dari {jumlah_halaman} halaman.")
        
    except PermissionError as e:
        # Jika file sedang dibuka oleh program lain (dikunci Windows), lemparkan errornya
        # ke atas agar file pengawas (watchdog) bisa mencoba memprosesnya lagi nanti.
        raise e
    except Exception as e:
        # Jika PDF rusak atau korup, hentikan proses di sini
        print(f"❌ [ERROR] Gagal membaca PDF {filename}: {e}")
        return
    
    # =====================================================================
    # FASE 2: MEMPERSIAPKAN OTAK AI UNTUK EKSTRAKSI JSON
    # =====================================================================
    model_extractor_name = "qwen3.5:4b" # Model cadangan jika settingan gagal dibaca
    
    # Membaca model apa yang disetel oleh HR di file konfigurasi (analytics_config.json)
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
                model_extractor_name = config_data.get("model_extractor", "qwen3.5:4b")
        except Exception:
            pass
            
    # Mencegah duplikasi data: Jika file dengan nama yang sama pernah diproses sebelumnya,
    # hapus dulu ingatan lama di VectorDB agar tidak double/bertabrakan saat ditimpa yang baru.
    try:
        vector_db.delete(where={"source": filename})
    except Exception:
        pass 
    
    # =====================================================================
    # FASE 3: JALUR TERSTRUKTUR (MASUK KE SQLITE)
    # =====================================================================
    # Menggabungkan semua halaman teks menjadi satu string panjang
    full_text = "\n".join(doc.page_content for doc in documents)
    
    # [OPTIMASI KOMPRESI]: Menghapus semua enter/garis baru dan spasi berlebih.
    # Tujuannya agar token yang dikirim ke AI lebih sedikit (menghemat RAM VGA/CPU).
    teks_untuk_json = re.sub(r'\s+', ' ', full_text).strip()
    
    # Mengirim teks yang sudah dikompresi ke fungsi 'update_database_catalog'.
    # Fungsi ini akan menyuruh AI mengubah teks jadi JSON (Nama, Umur, Skill) lalu menyimpannya ke tabel SQLite.
    update_database_catalog(filename, teks_untuk_json, model_extractor_name, False)
    
    # =====================================================================
    # FASE 4: JALUR TAK TERSTRUKTUR (MASUK KE VECTOR & BM25 UNTUK RAG)
    # =====================================================================
    # Memotong-motong dokumen penuh menjadi bagian-bagian kecil (chunk) sebesar 1000 karakter.
    # Ada 'overlap' 200 karakter agar kalimat yang terpotong di ujung tidak kehilangan konteks.
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    chunks = text_splitter.split_documents(documents)
    
    ids = []
    for i, chunk in enumerate(chunks):
        # Memberikan identitas (metadata) pada setiap potongan teks agar tahu teks ini asalnya dari CV siapa
        chunk.metadata["source"] = filename
        chunk.metadata["type"] = "resume"
        # Membuat ID unik (contoh: "user.pdf_0", "user.pdf_1")
        ids.append(f"{filename}_{i}")
    
    # 1. Simpan potongan teks ini ke dalam ChromaDB (Berbasis Makna/Vektor)
    vector_db.add_documents(chunks, ids=ids)
    print(f"-> [ChromaDB] Berhasil menyimpan {len(chunks)} chunk teks untuk {filename}!")
    
    # 2. Simpan potongan teks ini ke dalam Corpus BM25 (Berbasis Kata Kunci Eksak)
    bm25_corpus_path = Path(str(db_path)) / "bm25_corpus.pkl"
    try:
        corpus_docs = []
        # Jika file kumpulan kata kunci (pickle) sudah ada sebelumnya, buka dan baca dulu isinya
        if bm25_corpus_path.exists():
            with open(bm25_corpus_path, "rb") as f:
                corpus_docs = pickle.load(f)
                
        # Hapus potongan teks lama milik pelamar ini (jika ada) supaya data update CV tidak terhitung ganda
        corpus_docs = [doc for doc in corpus_docs if doc.metadata.get("source") != filename]
        
        # Tambahkan potongan teks (chunk) dari CV yang baru diproses ini ke dalam tumpukan corpus
        corpus_docs.extend(chunks)
        
        # Simpan/tutup kembali file pickle-nya
        with open(bm25_corpus_path, "wb") as f:
            pickle.dump(corpus_docs, f)
        print(f"-> [BM25 Corpus] Berhasil memperbarui keyword indeks untuk Hybrid Search!")
        
    except Exception as e:
        print(f"-> [Warning] Gagal memproses BM25 Corpus: {e}")

    print("=== Selesai ===\n")
    return True