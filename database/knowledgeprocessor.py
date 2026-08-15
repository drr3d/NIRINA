import os
import json
from pathlib import Path
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings, ChatOllama
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate

# --- 1. SETUP PATH & DATABASE (Sinkron dengan knowledgeprocessor.py/textprocessor.py) ---
app_dir = Path(__file__).resolve().parent
db_path = (app_dir / "../APPDB/chroma_db").resolve()
config_path = app_dir / "config.json"
environment_rules_path = app_dir / "environment_rules.json"

# Model embedding SAMA dengan knowledgeprocessor.py -- vektornya kompatibel,
# db_path juga SAMA (satu Chroma persist directory dipakai bersama semua
# domain), yang beda cuma collection_name di bawah.
embeddings = OllamaEmbeddings(model="nomic-embed-text", base_url="http://127.0.0.1:11434")

# --- KUNCI UTAMA: collection_name terpisah dari 'hr_knowledge'/'cv', dst ---
knowledge_db = Chroma(
    persist_directory=str(db_path),
    embedding_function=embeddings,
    collection_name="coding_knowledge"
)

# --- 2. NORMALISASI NAMA BAHASA ---
# Chroma filter metadata itu EXACT MATCH -- kalau saat ingest ditulis "C++"
# tapi saat generate ditulis "cpp", filter GAK BAKAL nemu apa-apa walau
# datanya ada. Semua nama bahasa WAJIB lewat sini dulu biar konsisten.
_ALIAS_BAHASA = {
    "c++": "cpp", "cpp": "cpp", "c/c++": "cpp",
    "py": "python", "python": "python", "python3": "python",
    "js": "javascript", "javascript": "javascript",
    "ts": "typescript", "typescript": "typescript",
    "golang": "go", "go": "go",
    "rs": "rust", "rust": "rust",
    "java": "java",
    "c": "c",
}

def normalisasi_bahasa(language: str) -> str:
    kunci = (language or "").strip().lower()
    return _ALIAS_BAHASA.get(kunci, kunci)


# --- 3. PROMPT GENERATOR KODE (DIPISAH DUA JALUR, SAMA POLA DENGAN knowledgeprocessor.py) ---
# Beda dari versi HR: {environment_rules} SELALU ada di kedua jalur (bukan
# cuma di jalur RAG) -- aturan compiler/versi/cara-jalanin itu TIDAK BOLEH
# bergantung pada apakah similarity search kebetulan nemu konteks atau tidak.

# PROMPT A: Jalur RAG (kalau referensi buku ADA & relevan)
prompt_dengan_buku = ChatPromptTemplate.from_messages([
    ("system",
     "Kamu adalah Senior Software Engineer yang menguasai banyak bahasa pemrograman. "
     "Tugasmu menulis kode SESUAI permintaan user untuk bahasa {language}.\n\n"
     "ATURAN PENALARAN (WAJIB DIIKUTI URUTANNYA):\n"
     "1. ATURAN ENVIRONMENT di bawah ini MUTLAK dan TIDAK BOLEH DILANGGAR -- itu bukan saran "
     "gaya penulisan, itu spesifikasi compiler/versi/cara-jalankan yang SEBENARNYA dipakai user. "
     "Kode yang gak sesuai ATURAN ENVIRONMENT dianggap SALAH walau secara sintaks benar.\n"
     "2. Ambil idiom/teknik/best-practice dari REFERENSI BUKU kalau relevan dengan permintaan.\n"
     "3. JANGAN comot kode mentah dari referensi -- adaptasikan ke kebutuhan spesifik user, dan "
     "tetap tunduk ke ATURAN ENVIRONMENT (referensi buku BUKAN sumber kebenaran soal environment).\n\n"
     "ATURAN ENVIRONMENT untuk {language} (MUTLAK):\n{environment_rules}\n\n"
     "FORMAT OUTPUT:\n"
     "- Kode lengkap dalam satu code block, siap dikompilasi/dijalankan APA ADANYA sesuai ATURAN ENVIRONMENT.\n"
     "- Penjelasan singkat kalau ada bagian yang perlu diperhatikan user."
    ),
    ("human",
     "REFERENSI BUKU (potongan relevan untuk {language}):\n{context}\n\n"
     "PERMINTAAN USER:\n{user_request}"
    )
])

# PROMPT B: Jalur Fallback (kalau belum ada referensi buku utk bahasa ini di DB)
prompt_tanpa_buku = ChatPromptTemplate.from_messages([
    ("system",
     "Kamu adalah Senior Software Engineer yang menguasai banyak bahasa pemrograman. "
     "Tugasmu menulis kode SESUAI permintaan user untuk bahasa {language}, menggunakan "
     "pengetahuan terbaikmu dan standar industri.\n\n"
     "ATURAN ENVIRONMENT untuk {language} (MUTLAK, TIDAK BOLEH DILANGGAR):\n{environment_rules}\n\n"
     "FORMAT OUTPUT:\n"
     "- Kode lengkap dalam satu code block, siap dikompilasi/dijalankan APA ADANYA sesuai ATURAN ENVIRONMENT.\n"
     "- Penjelasan singkat kalau ada bagian yang perlu diperhatikan user."
    ),
    ("human",
     "PERMINTAAN USER:\n{user_request}"
    )
])


# --- 4. FUNGSI INGEST REFERENSI/BUKU PEMROGRAMAN (Multi-bahasa) ---
def process_knowledge(file_path: str, language: str, start_page: int = 1) -> bool:
    """
    Membaca buku/referensi pemrograman (PDF) UNTUK SATU BAHASA TERTENTU,
    memotongnya jadi chunks, dan menyimpannya ke collection 'coding_knowledge'
    dengan metadata `language` -- supaya saat generate nanti, retrieval bisa
    difilter per bahasa (potongan C++ tidak akan nyasar ke request Python, dst).

    `language` WAJIB diisi (mis. "python", "cpp", "javascript") -- lihat
    normalisasi_bahasa() untuk daftar alias yang dikenali.
    """
    language = normalisasi_bahasa(language)
    if not language:
        print("❌ [ERROR] Parameter `language` wajib diisi (mis. 'python', 'cpp').")
        return False

    filename = os.path.basename(file_path)
    print(f"\n=== Memproses Referensi Coding [{language}]: {filename} ===")

    try:
        loader = PyPDFLoader(file_path)
        documents = loader.load()
        print(f"-> [Load Sukses] Dokumen terdiri dari {len(documents)} halaman.")

        # Skip halaman awal (cover, daftar isi, dll) -- sama seperti knowledgeprocessor.py
        filtered_documents = [
            doc for doc in documents
            if doc.metadata.get("page", 0) + 1 >= start_page
        ]

        if not filtered_documents:
            print(f"-> [Warning] Tidak ada halaman yang diproses karena start_page ({start_page}) melebihi total halaman PDF.")
            return False

        print(f"-> [Filter] Akan memproses {len(filtered_documents)} halaman (mulai dari halaman {start_page}).")

        # Hapus data lama untuk file yang sama agar tidak ada duplikasi vector
        try:
            knowledge_db.delete(where={"source": filename})
            print(f"-> [Clean Up] Menghapus data vector lama untuk file: {filename}")
        except Exception:
            pass

        # RecursiveCharacterTextSplitter tetap dipakai (bukan splitter khusus source-code)
        # karena input di sini adalah BUKU/PDF (prosa + contoh kode bercampur), bukan file
        # .py/.cpp mentah -- splitter berbasis paragraf lebih pas untuk konten begini.
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1200,
            chunk_overlap=250,
            length_function=len
        )
        chunks = text_splitter.split_documents(filtered_documents)

        ids = []
        for i, chunk in enumerate(chunks):
            chunk.metadata["source"] = filename
            chunk.metadata["type"] = "coding_knowledge"
            chunk.metadata["language"] = language  # <-- kunci filter saat retrieval
            chunk.metadata["page"] = chunk.metadata.get("page", 0) + 1

            unique_id = f"coding_{language}_{filename}_{i}"
            ids.append(unique_id)

        knowledge_db.add_documents(chunks, ids=ids)
        print(f"-> [ChromaDB] Berhasil menyimpan {len(chunks)} chunk referensi [{language}] untuk {filename}!")
        print("=== Selesai ===\n")
        return True

    except Exception as e:
        print(f"❌ [ERROR] Gagal memproses referensi coding: {e}")
        return False


# --- 5. ATURAN ENVIRONMENT (WAJIB, TIDAK BERGANTUNG PADA RETRIEVAL) ---
def _muat_environment_rules(language: str) -> str:
    """
    Baca aturan environment (compiler/versi/cara kompilasi/jalankan) untuk
    SATU bahasa dari environment_rules.json. Ini SELALU disuntik ke prompt,
    apapun hasil similarity search-nya -- constraint compiler/lingkungan user
    tidak boleh cuma "kebetulan ke-retrieve atau tidak".
    """
    if not environment_rules_path.exists():
        return "(Belum ada file environment_rules.json -- pakai standar umum bahasa ini.)"
    try:
        with open(environment_rules_path, "r", encoding="utf-8") as f:
            semua_aturan = json.load(f)
        aturan = semua_aturan.get(language)
        if not aturan:
            return f"(Belum ada aturan environment terdaftar untuk '{language}' -- pakai standar umum bahasa ini.)"
        return json.dumps(aturan, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"(Gagal membaca environment_rules.json: {e} -- pakai standar umum bahasa ini.)"

# PROMPT C: Ekstraksi query -- ubah narasi bebas user jadi istilah teknis yang
# selaras gaya buku referensi. Dipakai KHUSUS buat similarity_search, bukan
# buat generate kode -- generate kode tetap pakai user_request asli (lihat
# generate_code_solution).
prompt_ekstrak_query = ChatPromptTemplate.from_messages([
    ("system",
     "Kamu membantu proses pencarian di database referensi buku pemrograman {language}. "
     "Dari PERMINTAAN USER di bawah, ekstrak istilah/konsep TEKNIS {language} yang relevan "
     "untuk dicari (fitur bahasa, library, pola desain, teknik implementasi) -- BUKAN narasi "
     "kebutuhan bisnis/task-nya. Contoh: permintaan 'kirim email tiap jam 8 pagi' -> konsep "
     "teknisnya 'scheduling/timer, thread sleep, SMTP client, formatting tanggal-waktu'.\n\n"
     "Jawab HANYA dengan daftar istilah teknis dipisah koma, tanpa penjelasan, tanpa kalimat "
     "pembuka/penutup."
    ),
    ("human", "PERMINTAAN USER:\n{user_request}")
])

# --- 6. FUNGSI RAG UNTUK MENGHASILKAN KODE ---
def generate_code_solution(user_request: str, language: str) -> str:
    """
    Fungsi RAG yang dipanggil saat user minta dibuatkan/diperbaiki kode.
    Retrieval DIFILTER per `language` (jadi referensi C++ dan Python, dkk,
    tidak akan saling bercampur), dan aturan environment SELALU disuntik ke
    prompt terlepas dari hasil retrieval. Dilengkapi fallback Zero-Shot kalau
    belum ada referensi buku utk bahasa tersebut di database.
    """
    language = normalisasi_bahasa(language)
    if not language:
        return "Gagal: parameter `language` wajib diisi (mis. 'python', 'cpp')."

    # 1. Baca konfigurasi model aktif (key beda dari HR: 'model_coder')
    model_name = "qwen3.5:4b"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
                model_name = config_data.get("model_coder", "qwen3.5:4b")
        except Exception:
            pass

    # 2a. Ubah permintaan user (narasi bebas) jadi query pencarian yang selaras gaya
    # buku referensi (istilah teknis, bukan cerita kebutuhan) -- narasi task dan isi
    # buku referensi punya "gaya bahasa" beda, jadi similarity search ke query mentah
    # sering meleset walau datanya sebenarnya ada.
    query_pencarian = user_request
    try:
        query_llm = ChatOllama(model=model_name, temperature=0.3)
        hasil_ekstrak = (prompt_ekstrak_query | query_llm).invoke({
            "user_request": user_request,
            "language": language,
        })
        if hasil_ekstrak.content and hasil_ekstrak.content.strip():
            query_pencarian = hasil_ekstrak.content.strip()
    except Exception as e:
        print(f"-> [RAG] Gagal ekstraksi query ({e}), fallback pakai request asli.")

    print(f"-> [RAG] Mencari referensi [{language}] pakai query: '{query_pencarian}' (asli: '{user_request}')...")

    # 2b. Cari chunk relevan, DIFILTER metadata language
    docs = knowledge_db.similarity_search(query_pencarian, k=4, filter={"language": language})

    # 3. Aturan environment SELALU dimuat, independen dari hasil retrieval di atas.
    environment_rules = _muat_environment_rules(language)

    try:
        llm = ChatOllama(model=model_name, temperature=0.2)
        print(f"-> [AI Generator] Menyusun kode [{language}] menggunakan model: {model_name}...")

        if not docs:
            print(f"-> [RAG] Belum ada referensi [{language}] di database. Beralih ke pengetahuan bawaan (Zero-Shot)...")
            response = (prompt_tanpa_buku | llm).invoke({
                "user_request": user_request,
                "language": language,
                "environment_rules": environment_rules,
            })
        else:
            print(f"-> [RAG] Menemukan referensi [{language}] relevan. Memakai prompt dengan buku acuan.")
            context_list = []
            for doc in docs:
                source_file = doc.metadata.get("source", "Unknown")
                page_num = doc.metadata.get("page", "?")
                context_list.append(f"[Sumber: {source_file} - Hal. {page_num}]\n{doc.page_content}")
            context = "\n\n---\n\n".join(context_list)

            response = (prompt_dengan_buku | llm).invoke({
                "context": context,
                "user_request": user_request,
                "language": language,
                "environment_rules": environment_rules,
            })

        return response.content

    except Exception as e:
        return f"Gagal menghasilkan kode karena error: {e}"