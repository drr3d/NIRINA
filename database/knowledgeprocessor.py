import os
import json
import time
from pathlib import Path
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings, ChatOllama
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate

# --- 1. SETUP PATH & DATABASE (Sinkron dengan textprocessor.py) ---
app_dir = Path(__file__).resolve().parent
db_path = (app_dir / "../APPDB/chroma_db").resolve()
config_path = app_dir / "config.json"

# download using ollama pull bge-m3, ollama pull paraphrase-multilingual
# bge-m3 dan paraphrase-multilingual bagus untuk proses multilanguage data
# nomic-embed-text <- small untuk 1 bahasa bagus
embeddings = OllamaEmbeddings(model="paraphrase-multilingual", keep_alive=1800)

# --- KUNCI UTAMA: Menggunakan collection_name terpisah ---
knowledge_db = Chroma(
    persist_directory=str(db_path),
    embedding_function=embeddings,
    collection_name="document_knowledge"  # Dipisah agar tidak bercampur dengan collection lain (mis. CV)
)


# --- 2. PROMPT GENERATOR JAWABAN (DIPISAH DUA JALUR) ---
# PROMPT A: Jalur RAG (Jika dokumen relevan DITEMUKAN)
prompt_dengan_konteks = ChatPromptTemplate.from_messages([
    ("system",
     "Kamu adalah asisten yang menjawab pertanyaan HANYA berdasarkan potongan dokumen yang diberikan "
     "di bawah ini (KONTEKS). Dokumen ini berasal dari file yang di-upload oleh user.\n\n"
     "ATURAN PENALARAN (REASONING):\n"
     "1. JAWAB BERDASARKAN KONTEKS: Gunakan informasi dari KONTEKS sebagai sumber utama jawabanmu. "
     "Jangan mengarang informasi yang tidak ada di dalamnya.\n"
     "2. BOLEH MENYIMPULKAN: Kamu boleh merangkum, menghubungkan antar-bagian, atau menjelaskan ulang "
     "dengan bahasamu sendiri selama tetap didasarkan pada isi dokumen.\n"
     "3. JUJUR JIKA TIDAK CUKUP: Jika konteks yang diberikan tidak cukup untuk menjawab pertanyaan "
     "secara lengkap, katakan dengan jelas bagian mana yang tidak tercakup dalam dokumen.\n"
     "4. SERTAKAN SUMBER: Jika relevan, sebutkan nama file dan halaman sumber informasi yang kamu pakai.\n"
     "5. LINTAS BAHASA: KONTEKS bisa saja berbahasa Indonesia, Inggris, atau campuran keduanya, "
     "sedangkan PERTANYAAN USER bisa dalam bahasa yang berbeda dari KONTEKS. Tetap gunakan KONTEKS "
     "tersebut sebagai sumber jawaban meskipun bahasanya berbeda dari pertanyaan — jangan abaikan "
     "konteks hanya karena beda bahasa.\n"
     "6. BAHASA JAWABAN: Selalu jawab dalam bahasa yang sama dengan PERTANYAAN USER, terlepas dari "
     "bahasa KONTEKS aslinya. Terjemahkan/rangkum isi konteks ke bahasa pertanyaan user.\n\n"
     "FORMAT OUTPUT:\n"
     "- Jawaban langsung dan jelas terhadap pertanyaan user.\n"
     "- (Opsional) Referensi singkat: [Sumber: nama_file - Hal. X]"
    ),
    ("human",
     "KONTEKS DARI DOKUMEN:\n{context}\n\n"
     "PERTANYAAN USER:\n{user_request}"
    )
])

# PROMPT B: Jalur Fallback (Jika tidak ada dokumen relevan / database kosong)
prompt_tanpa_konteks = ChatPromptTemplate.from_messages([
    ("system",
     "Kamu adalah asisten yang membantu menjawab pertanyaan user.\n\n"
     "ATURAN:\n"
     "1. Tidak ditemukan potongan dokumen yang relevan di database untuk pertanyaan ini.\n"
     "2. Jawab menggunakan pengetahuan umummu semaksimal mungkin, dengan jelas dan ringkas.\n"
     "3. Beri tahu user secara eksplisit bahwa jawaban ini TIDAK berasal dari dokumen yang mereka "
     "upload, melainkan dari pengetahuan umum, agar user tidak salah kira.\n"
     "4. BAHASA JAWABAN: Selalu jawab dalam bahasa yang sama dengan PERTANYAAN USER.\n\n"
     "FORMAT OUTPUT:\n"
     "- Jawaban langsung dan jelas terhadap pertanyaan user.\n"
     "- Catatan singkat bahwa jawaban ini bukan berasal dari dokumen yang di-upload."
    ),
    ("human",
     "PERTANYAAN USER:\n{user_request}"
    )
])
# PROMPT C: Query Expansion (untuk retrieval lintas bahasa ID <-> EN)
prompt_query_variant = ChatPromptTemplate.from_messages([
    ("system",
     "Kamu adalah alat bantu pencarian. Tugasmu HANYA menerjemahkan pertanyaan user ke satu bahasa lain "
     "(kalau pertanyaannya berbahasa Indonesia, terjemahkan ke Inggris; kalau berbahasa Inggris, "
     "terjemahkan ke Indonesia). Jangan menjawab pertanyaannya, jangan menambahkan penjelasan apa pun. "
     "Balas HANYA dengan hasil terjemahannya saja, tanpa tanda kutip, tanpa embel-embel lain."
    ),
    ("human", "{user_request}")
])


# --- 3. FUNGSI UNTUK INGEST DOKUMEN (PDF apa pun, bebas topik) ---
def list_document_sources() -> list:
    """
    Mengembalikan daftar nama file unik yang sudah pernah di-ingest ke collection
    'document_knowledge'. Berguna untuk agent mengetahui dokumen apa saja yang tersedia,
    atau untuk menyapa/disambiguasi user kalau ada beberapa dokumen ter-upload.
    """
    try:
        raw = knowledge_db.get(include=["metadatas"])
        sources = {m.get("source") for m in raw.get("metadatas", []) if m.get("source")}
        return sorted(sources)
    except Exception as e:
        print(f"-> [ERROR] Gagal mengambil daftar dokumen: {e}")
        return []

def process_document_knowledge(file_path: str, start_page: int = 1) -> bool:
    """
    Fungsi untuk membaca dokumen PDF yang di-upload user, memotongnya menjadi chunks,
    dan menyimpannya ke dalam collection 'document_knowledge'.
    Dilengkapi dengan fitur skip halaman (start_page) untuk efisiensi komputasi
    (mis. melewati cover/daftar isi).
    """
    filename = os.path.basename(file_path)
    print(f"\n=== Memproses Dokumen: {filename} ===")

    try:
        # Load PDF dokumen
        loader = PyPDFLoader(file_path)
        documents = loader.load()
        print(f"-> [Load Sukses] Dokumen terdiri dari {len(documents)} halaman.")

        # Filter dokumen untuk skip halaman awal (cover, daftar isi, dll)
        # Note: documents[i].metadata["page"] adalah 0-indexed dari PyPDFLoader
        # Jadi doc.metadata.get("page", 0) + 1 adalah halaman aktual yang sesuai dengan mata manusia
        filtered_documents = [
            doc for doc in documents
            if doc.metadata.get("page", 0) + 1 >= start_page
        ]

        # Validasi jika user memasukkan start_page yang melebihi jumlah halaman PDF
        if not filtered_documents:
            print(f"-> [Warning] Tidak ada halaman yang diproses karena start_page ({start_page}) melebihi total halaman PDF.")
            return False

        print(f"-> [Filter] Akan memproses {len(filtered_documents)} halaman (mulai dari halaman {start_page}).")

        # Hapus data lama untuk file yang sama agar tidak ada duplikasi vector (Vector Overwrite Protection)
        try:
            knowledge_db.delete(where={"source": filename})
            print(f"-> [Clean Up] Menghapus data vector lama untuk file: {filename}")
        except Exception:
            pass

        # Split dokumen menjadi potongan kecil (chunk_size sedikit lebih besar agar dapet konteks utuh)
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1200,
            chunk_overlap=250,
            length_function=len
        )
        chunks = text_splitter.split_documents(filtered_documents)

        # Berikan metadata khusus pada setiap chunk
        ids = []
        for i, chunk in enumerate(chunks):
            chunk.metadata["source"] = filename
            chunk.metadata["type"] = "document_knowledge"
            # Pastikan format page untuk RAG transparan mulai dari index 1
            chunk.metadata["page"] = chunk.metadata.get("page", 0) + 1

            # Pembuatan ID unik untuk kemudahan manajemen database (Delete/Update)
            unique_id = f"knowledge_{filename}_{i}"
            ids.append(unique_id)

        # --- SIMPAN KE CHROMADB SECARA BER-BATCH + RETRY (biar robust untuk dokumen besar) ---
        # Dulu semua chunk dikirim dalam SATU panggilan add_documents() -- untuk dokumen
        # ratusan halaman ini bisa jadi ribuan chunk berturut-turut tanpa jeda ke Ollama.
        # Kalau di tengah jalan Ollama sempat bermasalah (unload/reload model, dsb),
        # SELURUH proses gugur dan progres yang sudah berhasil pun ikut hilang karena baru
        # "dianggap selesai" di akhir. Sekarang dipecah per-batch kecil, tiap batch di-retry
        # sendiri-sendiri kalau gagal, dan progres per-batch langsung ke-commit ke Chroma
        # (jadi kalaupun akhirnya berhenti di tengah, chunk yang sudah masuk TETAP tersimpan).
        BATCH_SIZE = 40
        MAX_RETRIES = 3
        RETRY_DELAY_SECONDS = 8  # naik tiap percobaan (linear backoff sederhana)

        total_chunks = len(chunks)
        total_batches = (total_chunks + BATCH_SIZE - 1) // BATCH_SIZE
        chunks_gagal = []  # simpan (chunk, id) yang gagal permanen buat dilaporkan di akhir

        for batch_idx in range(total_batches):
            start_i = batch_idx * BATCH_SIZE
            end_i = min(start_i + BATCH_SIZE, total_chunks)
            batch_chunks = chunks[start_i:end_i]
            batch_ids = ids[start_i:end_i]

            berhasil = False
            for percobaan in range(1, MAX_RETRIES + 1):
                try:
                    knowledge_db.add_documents(batch_chunks, ids=batch_ids)
                    berhasil = True
                    break
                except Exception as e:
                    print(
                        f"-> [Warning] Batch {batch_idx + 1}/{total_batches} gagal "
                        f"(percobaan {percobaan}/{MAX_RETRIES}): {e}"
                    )
                    if percobaan < MAX_RETRIES:
                        jeda = RETRY_DELAY_SECONDS * percobaan
                        print(f"-> [Retry] Menunggu {jeda}s sebelum coba lagi...")
                        time.sleep(jeda)

            if berhasil:
                print(
                    f"-> [ChromaDB] Batch {batch_idx + 1}/{total_batches} tersimpan "
                    f"({end_i}/{total_chunks} chunk)."
                )
            else:
                print(
                    f"-> [ERROR] Batch {batch_idx + 1}/{total_batches} GAGAL permanen "
                    f"setelah {MAX_RETRIES}x percobaan. {len(batch_chunks)} chunk dilewati."
                )
                chunks_gagal.extend(batch_ids)

            # Jeda kecil antar-batch supaya Ollama sempat "napas" (bantu cegah numpuknya
            # resource/memory saat batch panjang berturut-turut).
            time.sleep(1)

        chunks_berhasil = total_chunks - len(chunks_gagal)
        print(f"-> [ChromaDB] Selesai: {chunks_berhasil}/{total_chunks} chunk tersimpan untuk {filename}.")

        if chunks_gagal:
            print(
                f"⚠️ [Partial] {len(chunks_gagal)} chunk gagal disimpan meski sudah di-retry "
                f"{MAX_RETRIES}x. Jalankan ulang process_document_knowledge() untuk file yang "
                f"sama kalau ingin coba lagi (chunk yang sudah berhasil tidak akan diulang "
                f"karena delete-and-replace di awal fungsi akan menghapus semuanya dan memproses "
                f"ulang dari nol)."
            )

        print("=== Selesai ===\n")
        # Dianggap sukses kalau MINIMAL ada satu chunk yang berhasil tersimpan --
        # panggil list_document_sources() atau cek log di atas untuk tau apakah ada yang
        # gagal sebagian (partial success).
        return chunks_berhasil > 0

    except Exception as e:
        print(f"❌ [ERROR] Gagal memproses dokumen: {e}")
        return False


# --- 4. FUNGSI RAG UNTUK MENJAWAB PERTANYAAN UMUM TENTANG DOKUMEN ---
def generate_document_answer(user_request: str, source_filter: str = None) -> str:
    """
    Fungsi RAG yang dipanggil saat user bertanya tentang isi dokumen yang di-upload.
    Mengambil konteks dari database knowledge, lalu melemparnya ke LLM lokal.
    Dilengkapi dengan fallback Zero-Shot jika database kosong / tidak ada yang relevan.

    source_filter (opsional): nama file spesifik (harus persis sama seperti hasil
    list_document_sources()). Kalau diisi, pencarian dibatasi HANYA ke dokumen itu.
    Kalau None (default), pencarian dilakukan lintas SEMUA dokumen yang ter-upload.
    """
    # 1. Baca konfigurasi model aktif
    model_name = "qwen3.5:4b"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
                model_name = config_data.get("model_extractor", "qwen3.5:4b")
        except Exception:
            pass

    print(f"-> [RAG] Mencari konteks relevan untuk pertanyaan: '{user_request}'..."
          + (f" (dibatasi ke file: {source_filter})" if source_filter else ""))

    # 2. Siapkan LLM lokal lebih dulu (dipakai untuk query-expansion & generasi jawaban)
    llm = ChatOllama(model=model_name, temperature=0.2)

    search_kwargs = {"filter": {"source": source_filter}} if source_filter else {}

    # 2a. Cari chunk relevan pakai query asli
    docs = knowledge_db.similarity_search(user_request, k=4, **search_kwargs)

    # 2b. QUERY EXPANSION: buat 1 variasi query dalam bahasa "lawan" (ID<->EN).
    # Ini penting karena embedding model (meski sudah multilingual) tetap bisa lebih akurat
    # kalau query dan dokumen berada di bahasa yang sama. Dengan menambah pencarian pakai
    # query hasil terjemahan, dokumen berbahasa lain jadi lebih mudah ketemu.
    try:
        variant_query = (prompt_query_variant | llm).invoke({
            "user_request": user_request
        }).content.strip()

        if variant_query and variant_query.lower() != user_request.strip().lower():
            print(f"-> [RAG] Query variant (lintas bahasa): '{variant_query}'")
            docs_variant = knowledge_db.similarity_search(variant_query, k=4, **search_kwargs)

            # Gabungkan hasil pencarian original + variant, dedupe berdasarkan (source, page, isi)
            seen = {(d.metadata.get("source"), d.metadata.get("page"), d.page_content) for d in docs}
            for d in docs_variant:
                key = (d.metadata.get("source"), d.metadata.get("page"), d.page_content)
                if key not in seen:
                    docs.append(d)
                    seen.add(key)
    except Exception as e:
        # Kalau query expansion gagal (mis. LLM error), lanjut saja pakai hasil query original
        print(f"-> [RAG] Query expansion dilewati karena error: {e}")

    # Batasi jumlah chunk konteks yang dikirim ke LLM biar tidak kebanyakan
    docs = docs[:6]

    # 3. Panggil LLM lokal (dengan temperature rendah agar patuh pada isi dokumen)
    try:
        print(f"-> [AI Generator] Menyusun jawaban menggunakan model: {model_name}...")

        # Fallback ke pengetahuan bawaan AI jika DB kosong atau tidak relevan
        if not docs:
            print("-> [RAG] Tidak ada dokumen relevan ditemukan. Beralih ke pengetahuan bawaan (Zero-Shot)...")
            response = (prompt_tanpa_konteks | llm).invoke({
                "user_request": user_request
            })
        else:
            print("-> [RAG] Menemukan konteks relevan. Memakai prompt dengan dokumen acuan.")
            # Gabungkan dokumen yang relevan beserta informasi halaman untuk transparansi
            context_list = []
            for doc in docs:
                source_file = doc.metadata.get("source", "Unknown")
                page_num = doc.metadata.get("page", "?")
                context_list.append(f"[Sumber: {source_file} - Hal. {page_num}]\n{doc.page_content}")

            context = "\n\n---\n\n".join(context_list)

            response = (prompt_dengan_konteks | llm).invoke({
                "context": context,
                "user_request": user_request
            })

        return response.content

    except Exception as e:
        # Menangkap dan mengembalikan pesan error dengan anggun (graceful degradation)
        return f"Gagal menghasilkan jawaban karena error: {e}"