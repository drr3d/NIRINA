import re
import hashlib
import time
from langchain_core.tools import tool
from langchain_core.messages import AIMessage
from collections import defaultdict

import chromadb
from chromadb.utils import embedding_functions

# {"tools_reward", "tools_gagal", "tools_batal", "lupakan_skill_gagal"} # consider move this to config.json
default_tools = set() # Jika default tools mau diisi, maka gunakan format dict seperti diatas

class ToolRegistry:
    """Registry framework dinamis dengan Backward Compatibility penuh + Tool-RAG."""
    _tools = defaultdict(list)
    _internal_tools = {}

    # --- TAMBAHAN UNTUK TOOL-RAG (GORILLA STYLE) ---
    _chroma_client = chromadb.PersistentClient(path="./chroma_db_nirina")
    _embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="paraphrase-multilingual-MiniLM-L12-v2")
    _collection = _chroma_client.get_or_create_collection(name="tool_library", embedding_function=_embed_fn)

    _tools_wajib_selalu = default_tools
    # -----------------------------------------------

    # --- TAMBAHAN: CONTEXTUAL RETRIEVAL (Anthropic Technical Report, Sept 2024) ---
    _contextual_cache = {}

    # --- FITUR BARU: MODE RETRIEVAL DEFAULT GLOBAL ("gorilla" | "hyde" | "contextual") ---
    _default_retrieval_mode = "fusion"

    # Database intent sintetis task kompleks (bisa ditambah/diatur dinamis)
    # Should be moved to config.json in the future
    _synthetic_intents = {}

    @classmethod
    def set_default_retrieval_mode(cls, mode: str):
        """Mengubah mode pencarian default secara global ('gorilla', 'hyde',
        'contextual', atau 'fusion' -- lihat catatan di `_default_retrieval_mode`
        kenapa 'fusion' yang direkomendasikan)."""
        if mode.lower() in ["gorilla", "hyde", "contextual", "fusion"]:
            cls._default_retrieval_mode = mode.lower()
            print(f"[ToolRegistry] Mode retrieval default diubah ke: {cls._default_retrieval_mode.upper()}")
        else:
            raise ValueError("Mode retrieval harus 'gorilla', 'hyde', 'contextual', atau 'fusion'.")

    @classmethod
    def daftar_tool_wajib(cls, *nama_tool: str):
        """Tandai satu/lebih nama tool supaya SELALU ikut di get_relevant_tools,
        berapapun top_k-nya dan apapun hasil semantic search-nya. Dipanggil dari
        agent_factory.py (atau file plugin) saat startup -- bukan hardcode di
        sini -- supaya menambah tool kontrol baru tidak perlu sentuh registry.py."""
        cls._tools_wajib_selalu.update(nama_tool)

    @classmethod
    def register(cls, is_sensitive: bool = None, category: str = None, is_internal: bool = False,
                 intents: str = None):

        if category is None:
            target_category = "sensitive" if is_sensitive else "safe"
        else:
            target_category = category

        def decorator(func):
            langchain_tool = tool(func)
            langchain_tool.metadata = {
                **(langchain_tool.metadata or {}),
                "category": target_category,
                "intents": intents or "",
            }
            if is_internal:
                cls._internal_tools[langchain_tool.name] = langchain_tool
            else:
                cls._tools[target_category].append(langchain_tool)
            return langchain_tool
        return decorator

    @classmethod
    def get_tools(cls, category: str):
        return cls._tools.get(category, [])

    @classmethod
    def get_all_tools(cls):
        """Menggabungkan semua kategori untuk Otak LLM."""
        all_tools = []
        for tool_list in cls._tools.values():
            all_tools.extend(tool_list)
        return all_tools

    @classmethod
    def get_all_automation_tools(cls):
        """Mengembalikan SELURUH tools (LLM tools + Internal tools) untuk Tab Automation UI."""
        all_tools = cls.get_all_tools()
        all_tools.extend(list(cls._internal_tools.values()))
        return all_tools

    # =========================================================
    # SINKRONISASI & PENCARIAN ALAT (AMAN & TERPISAH)
    # =========================================================
    @classmethod
    def _ambil_intents(cls, tool) -> str:
        """Union intents dari metadata tool (di-set lewat kwarg `intents=` saat
        register) DENGAN dict lama `_synthetic_intents` -- supaya tool lama yang
        belum dimigrasi tetap dapat boost, tapi tool baru tidak wajib edit
        registry.py."""
        dari_metadata = (getattr(tool, "metadata", {}) or {}).get("intents", "")
        dari_dict_lama = cls._synthetic_intents.get(tool.name, "")
        gabungan = ", ".join(x for x in [dari_metadata, dari_dict_lama] if x)
        return gabungan

    @staticmethod
    def _hash_docstring(teks: str) -> str:
        """Hash pendek dari docstring -- dipakai buat deteksi 'apakah docstring
        tool ini berubah sejak terakhir di-generate ringkasan kontekstualnya'."""
        return hashlib.sha256((teks or "").encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _estimasi_token_kasar(teks: str) -> int:
        """Estimasi token KASAR (heuristik char/3.5, SAMA dgn fallback
        DynamicTokenRouterLLM di agent_factory.py) -- registry.py sengaja
        tidak import tiktoken (dependensi berat) cuma buat logging, jadi
        angka ini perkiraan, BUKAN hitungan presisi tokenizer asli."""
        return int(len(teks or "") / 3.5)

    @staticmethod
    def _deskripsi_llm(llm) -> str:
        """Nama pendek buat identifikasi DI LOG mana LLM yang sedang dipanggil
        (mis. 'summarizer'/'qwen2.5:3b' vs LLM lain) -- coba beberapa atribut
        umum LangChain/objek custom sebelum fallback ke nama class."""
        if llm is None:
            return "None"
        for atr in ("nama", "model", "model_name"):
            val = getattr(llm, atr, None)
            if val:
                return str(val)
        return type(llm).__name__

    @classmethod
    def _generate_contextual_summary(cls, tool, llm=None) -> str:
        """
        [Contextual Retrieval -- Anthropic Technical Report, Sept 2024, prinsip #1]
        Generate ringkasan kontekstual SEKALI per tool, di INDEX-TIME (dipanggil
        dari `sync_tools_to_db`), BUKAN di setiap query seperti HyDE.

        Ini BUKAN menambal "chunk kehilangan konteks parent" (masalah asli di
        paper) -- docstring tool di sini sudah atomic, bukan potongan dokumen
        besar. Yang applicable justru prinsip index-time generation-nya: dipakai
        buat MERINGKAS docstring besar/teknis (keluhan awal) jadi kalimat padat
        berfokus intent pengguna, supaya tidak mengencerkan embedding dengan
        detail parameter/format-return yang jarang muncul di query natural-
        language pengguna.

        Di-cache berdasar hash docstring saat ini -- kalau docstring tool tidak
        berubah sejak generate terakhir, TIDAK di-generate ulang (zero cost per
        sync). Kalau `llm=None` (default), no-op murni -- dokumen index tetap
        persis seperti sebelum fitur ini ada (backward compatible penuh), sama
        filosofi fallback seperti `_generate_hyde_doc`.
        """
        docstring_asli = tool.description or ""
        hash_sekarang = cls._hash_docstring(docstring_asli)
        cached = cls._contextual_cache.get(tool.name)

        if cached and cached.get("hash") == hash_sekarang and cached.get("generated"):
            return cached.get("context", "")

        if llm is None:
            if not (cached and cached.get("hash") == hash_sekarang):
                cls._contextual_cache[tool.name] = {
                    "hash": hash_sekarang, "context": "", "generated": False,
                }
            return cls._contextual_cache.get(tool.name, {}).get("context", "")

        prompt = (
            f"Ringkas fungsi tool berikut dalam 1-2 kalimat singkat, fokus pada "
            f"KAPAN/UNTUK APA tool ini dipakai (intent pengguna), BUKAN detail "
            f"parameter teknis atau format return.\n\n"
            f"Nama Tool: {tool.name}\n"
            f"Deskripsi asli: {docstring_asli}\n\n"
            f"Jawab langsung tanpa mukadimah, cukup 1-2 kalimat ringkasan."
        )
        tok_prompt = cls._estimasi_token_kasar(prompt)
        try:
            response = llm.invoke(prompt)
            ringkasan = response.content if hasattr(response, "content") else str(response)
            ringkasan = ringkasan.strip()
            berhasil = True
            tok_hasil = cls._estimasi_token_kasar(ringkasan)
            print(
                f"[📝 Contextual-Index] '{tool.name}' via llm={cls._deskripsi_llm(llm)} -- "
                f"prompt≈{tok_prompt} token, hasil≈{tok_hasil} token -> \"{ringkasan[:80]}\""
            )
        except Exception as e:
            print(f"⚠️ [Contextual-Index] Gagal generate ringkasan untuk '{tool.name}' (prompt≈{tok_prompt} token), skip: {e}")
            ringkasan = ""
            berhasil = False

        cls._contextual_cache[tool.name] = {
            "hash": hash_sekarang, "context": ringkasan, "generated": berhasil,
        }
        return ringkasan

    @classmethod
    def sync_tools_to_db(cls, llm=None, force_regenerate: bool = False):
        """Sinkronisasi deskripsi tool publik ke ChromaDB saat startup.

        `llm` (opsional, default None): kalau dioper, setiap tool dapat
        ringkasan kontekstual tambahan yang ikut di-embed (lihat
        `_generate_contextual_summary`) -- di-generate SEKALI per docstring
        unik lalu di-cache selama proses hidup (Contextual Retrieval,
        Anthropic 2024). Kalau `llm=None`, perilaku PERSIS seperti sebelum
        fitur ini ada -- backward compatible penuh.

        `force_regenerate` (default False): buang cache ringkasan dulu sebelum
        sync -- pakai kalau baru habis edit massal banyak docstring sekaligus
        dan mau paksa re-generate semua (biasanya tidak perlu, karena
        perubahan docstring per-tool sudah otomatis terdeteksi lewat hash).
        """
        if force_regenerate:
            cls._contextual_cache.clear()

        t0 = time.time()
        ids = []
        documents = []
        
        # Ekstrak dari objek langchain_tool (yang punya atribut .name dan .description)
        for t in cls.get_all_tools():
            ids.append(t.name)
            
            metadata_dict = getattr(t, "metadata", {}) or {}
            kategori = metadata_dict.get("category", "")
            intents = cls._ambil_intents(t)
            
            # Rangkai nama dan deskripsi untuk jadi embedding pencarian
            doc_content = f"Nama Tool: {t.name}\nDeskripsi: {t.description}\nKategori: {kategori}\nPemicu Pentest: {intents}"

            ringkasan_kontekstual = cls._generate_contextual_summary(t, llm=llm)
            if ringkasan_kontekstual:
                doc_content += f"\nKonteks: {ringkasan_kontekstual}"

            documents.append(doc_content)
        
        if ids:
            cls._collection.upsert(documents=documents, ids=ids)
            jumlah_dengan_konteks = sum(
                1 for name in ids if cls._contextual_cache.get(name, {}).get("context")
            )
            elapsed = time.time() - t0
            print(
                f"[Tool-RAG] {len(ids)} tools berhasil disinkronisasi ke memori. "
                f"({jumlah_dengan_konteks} dengan ringkasan kontekstual, "
                f"llm={cls._deskripsi_llm(llm)}, {elapsed:.2f} detik total)"
            )

    @staticmethod
    def _tokenize(teks: str) -> set:
        return set(re.findall(r"[a-z0-9]+", (teks or "").lower()))

    @classmethod
    def _hitung_df_token_nama(cls, all_tools) -> dict:
        df = defaultdict(int)
        for t in all_tools:
            for tok in cls._tokenize(t.name):
                df[tok] += 1
        return df

    @classmethod
    def _skor_lexical(cls, query_tokens: set, tool) -> float:
        """Skor keyword/lexical sederhana (token overlap, bukan BM25 penuh --
        sengaja tanpa dependency tambahan) antara query & (nama+deskripsi) tool.

        KENAPA PERLU, PADAHAL SUDAH ADA SEMANTIC SEARCH:
        Embedding model kecil (all-MiniLM-L6-v2) bagus buat kemiripan MAKNA umum,
        tapi sering false-negative untuk istilah teknis SPESIFIK. Skor
        dinormalisasi ke overlap/len(query_tokens) supaya query panjang tidak
        otomatis unggul dibanding query pendek yang presisi. Dipakai HANYA untuk
        ranking "lemah"/tambahan -- klasifikasi kuat/lemah exact-match sendiri
        pakai `_klasifikasi_exact_match` di bawah, bukan skor ini.
        """
        if not query_tokens:
            return 0.0
            
        intents = cls._ambil_intents(tool)
        tool_tokens = cls._tokenize(f"{tool.name} {tool.description or ''} {intents}")
        
        if not tool_tokens:
            return 0.0
        overlap = query_tokens & tool_tokens
        if not overlap:
            return 0.0
        nama_tokens = cls._tokenize(tool.name)
        bonus_nama = 0.5 if (query_tokens & nama_tokens) else 0.0
        return (len(overlap) / len(query_tokens)) + bonus_nama

    @classmethod
    def _klasifikasi_exact_match(cls, query_tokens: set, tool, df_token: dict, ambang_df: int = 1,
                                  ambang_cakupan: float = 0.66):
        """Klasifikasikan exact-name-match tool ini jadi "kuat" atau "lemah".

        KUAT (dijamin masuk, TIDAK dibatasi top_k -- mirip _tools_wajib_selalu) kalau:
          (a) ADA token overlap yang df-nya <= ambang_df (kata itu cuma dipunyai
              ambang_df nama tool atau kurang -- default 1, artinya BENAR-BENAR
              unik milik tool ini), ATAU
          (b) cakupan overlap terhadap SELURUH token nama tool >= ambang_cakupan
              (mayoritas kata di nama tool itu ada di query, meski masing-masing
              kata sendirian generik -- mis. query bilang "scan port nmap" persis
              menutupi ke-3 kata nama tool "scan_port_nmap").

        LEMAH (exact-match tetap tercatat, tapi tunduk ke kompetisi RRF biasa
        dan bisa ke-drop kalau slot penuh) kalau overlap ADA tapi TIDAK memenuhi
        (a) maupun (b) -- biasanya cuma nyantol 1 kata generik doang, mis. "buat"
        yang dipunyai banyak tool sekaligus (buat_exploit, buat_sqli_exploit).

        Return: "kuat" | "lemah" | None (None = tidak exact-match sama sekali).
        """
        nama_tokens = cls._tokenize(tool.name)
        overlap_nama = query_tokens & nama_tokens
        
        if not overlap_nama:
            return None
            
        # 1. Syarat Cakupan Mayoritas (mayoritas token nama tool harus ada di
        # kueri -- ambang aktual dikontrol parameter `ambang_cakupan`, default
        # 0.66 alias 66%)
        cakupan = len(overlap_nama) / len(nama_tokens) if nama_tokens else 0
        if cakupan >= ambang_cakupan:
            return "kuat"
            
        # 2. Syarat Token Unik (Diperketat!)
        # Token harus unik (df <= ambang_df) DAN memiliki panjang lebih dari 3 huruf (mencegah "di", "ke")
        token_unik_valid = [tok for tok in overlap_nama if df_token.get(tok, 0) <= ambang_df and len(tok) > 3]
        
        if token_unik_valid:
            # Jika nama tool pendek (1-2 kata), 1 token unik panjang sudah cukup untuk jadi bukti kuat
            if len(nama_tokens) <= 2:
                return "kuat"
            else:
                overlap_bermakna = [tok for tok in overlap_nama if len(tok) > 3]
                if len(overlap_bermakna) >= 2:
                    return "kuat"
                
        # Jika gagal melewati syarat ketat di atas, turunkan kasta menjadi "lemah"
        return "lemah"

    _hyde_cache = {}
    _HYDE_CACHE_MAX = 256

    @classmethod
    def _hyde_cache_get(cls, key: str):
        if key in cls._hyde_cache:
            val = cls._hyde_cache.pop(key)
            cls._hyde_cache[key] = val  # sentuh ulang -> paling baru
            return val
        return None

    @classmethod
    def _hyde_cache_put(cls, key: str, value: str):
        if key in cls._hyde_cache:
            cls._hyde_cache.pop(key)
        elif len(cls._hyde_cache) >= cls._HYDE_CACHE_MAX:
            cls._hyde_cache.pop(next(iter(cls._hyde_cache)))  # buang entri tertua
        cls._hyde_cache[key] = value

    @classmethod
    def _generate_hyde_doc(cls, task_query: str, llm=None, all_tools=None) -> str:
        """
        [HyDE Engine] Transformasi kueri tugas pengguna (yang seringkali
        pendek/ambigu) menjadi deskripsi fungsi hipotetis ideal, di-embed
        ke ChromaDB berdampingan dengan embedding kueri asli.

        """
        cache_key = (task_query or "").strip().lower()
        cached = cls._hyde_cache_get(cache_key)
        if cached is not None:
            print(f"[🧭 HyDE] cache HIT untuk query '{task_query[:50]}' -- 0 panggilan LLM.")
            return cached

        if llm is None:
            print(
                "⚠️ [HyDE] mode='hyde'/'fusion' aktif tapi `llm` TIDAK dioper ke "
                "get_relevant_tools() -- HyDE otomatis NO-OP, fallback ke kueri "
                "asli. Cek pemanggil (mis. retrieve() di agent_nodes.py) apakah "
                "lupa meneruskan llm=..."
            )
            return task_query

        petunjuk_nama_tool = ""
        if all_tools:
            nama_tools = sorted({t.name for t in all_tools})[:40]  # cap murah
            petunjuk_nama_tool = (
                f"Sebagai referensi gaya penamaan, beberapa tool yang SUDAH ADA "
                f"di pustaka ini bernama: {', '.join(nama_tools)}. Tidak wajib "
                f"memilih salah satu, tapi usahakan gaya istilah teknis Anda "
                f"konsisten dengan vocabulary tool-tool tersebut.\n"
            )

        prompt = (
            f"Anda adalah sistem perancang pustaka fungsi/tool.\n"
            f"Tugas Pengguna: '{task_query}'\n"
            f"{petunjuk_nama_tool}"
            f"Tuliskan deskripsi ringkas teknis mengenai tool Python yang ideal untuk menyelesaikan tugas di atas.\n"
            f"Format jawaban (langsung tanpa mukadimah):\n"
            f"Nama Tool: <nama_hipotetis>\n"
            f"Deskripsi: <penjelasan_fungsi_dan_kemampuannya>"
        )
        tok_prompt = cls._estimasi_token_kasar(prompt)
        try:
            response = llm.invoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)
            content = content.strip() or task_query
            print(
                f"[🧭 HyDE] cache MISS -- panggil llm={cls._deskripsi_llm(llm)} -- "
                f"prompt≈{tok_prompt} token (termasuk {len(all_tools) if all_tools else 0} nama tool, "
                f"di-cap 40), hasil≈{cls._estimasi_token_kasar(content)} token"
            )
        except Exception as e:
            print(f"⚠️ [HyDE] Gagal generate hypothetical doc (prompt≈{tok_prompt} token), fallback ke kueri asli: {e}")
            content = task_query

        cls._hyde_cache_put(cache_key, content)
        return content

    @classmethod
    def _rerank_with_llm(cls, task_query: str, kandidat: list, llm, slot_tersisa: int) -> list:
        """
        [Contextual Retrieval -- Anthropic Technical Report, Sept 2024, prinsip #2]
        LLM reranking atas kandidat yang SUDAH lolos RRF -- ini lever TERBESAR
        di paper Anthropic (-67% retrieval failure dgn rerank, vs -49% tanpa
        rerank, vs 0% baseline). Candidate pool di sistem ini kecil (belasan/
        puluhan tool), jadi murah & cepat buat di-rerank pakai LLM.

        BEDA dengan ringkasan kontekstual di atas: itu cost-nya SEKALI di
        index-time (zero cost per giliran). Ini tetap 1x panggilan LLM per
        giliran DI RETRIEVAL-TIME -- trade-off latency vs akurasi yang sama
        seperti HyDE. Makanya OPT-IN lewat parameter `rerank=True` di
        `get_relevant_tools`, bukan default.

        FAIL-SAFE: kalau llm error atau responsnya tidak bisa di-parse jadi
        urutan nomor valid, kembalikan `kandidat` apa adanya (urutan RRF
        asli) -- reranking yang gagal TIDAK BOLEH menjatuhkan retrieval.
        """
        if not kandidat or llm is None:
            return kandidat

        daftar = "\n".join(f"{i+1}. {t.name}: {t.description}" for i, t in enumerate(kandidat))
        prompt = (
            f"Tugas Pengguna: '{task_query}'\n\n"
            f"Berikut daftar tool kandidat (sudah pra-filter oleh sistem lain):\n{daftar}\n\n"
            f"Urutkan NOMOR tool dari yang PALING relevan ke PALING TIDAK relevan "
            f"untuk menyelesaikan Tugas Pengguna di atas. Jawab HANYA dengan daftar "
            f"nomor dipisah koma, tanpa penjelasan apa pun. Contoh: 3,1,2"
        )
        tok_prompt = cls._estimasi_token_kasar(prompt)
        try:
            response = llm.invoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)
            print(
                f"[🎯 Contextual-Rerank] {len(kandidat)} kandidat via llm={cls._deskripsi_llm(llm)} -- "
                f"prompt≈{tok_prompt} token"
            )
            urutan_nomor = [int(x) for x in re.findall(r"\d+", content)]

            terpakai = set()
            hasil_rerank = []
            for nomor in urutan_nomor:
                idx = nomor - 1
                if 0 <= idx < len(kandidat) and idx not in terpakai:
                    hasil_rerank.append(kandidat[idx])
                    terpakai.add(idx)

            # Selipkan sisa kandidat yang tidak disebut LLM (jaga2 parsing parsial)
            for i, t in enumerate(kandidat):
                if i not in terpakai:
                    hasil_rerank.append(t)

            return hasil_rerank[:slot_tersisa] if hasil_rerank else kandidat
        except Exception as e:
            print(f"⚠️ [Contextual-Rerank] Gagal rerank (prompt≈{tok_prompt} token), fallback ke urutan RRF asli: {e}")
            return kandidat

    @classmethod
    def get_relevant_tools(cls, task_query: str, top_k: int = 3, 
                           bobot_semantic: float = 0.65, 
                           ambang_df: int = 1, ambang_cakupan: float = 0.75,
                           mode: str = None, llm = None, phase: str = None,
                           rerank: bool = False):
        """[HYBRID] Filter dinamis tool untuk disuapkan ke LLM -- gabungan semantic
        search (ChromaDB embedding, nangkep kemiripan MAKNA) + lexical/keyword
        exact-match (nangkep istilah teknis SPESIFIK yang sering dilewatkan
        embedding model kecil), digabung lewat 3 lapis:

        LAPIS 1 -- PROMOSI KERAS untuk EXACT-MATCH "KUAT" (lihat
        `_klasifikasi_exact_match`): tool yang exact-match ke kata UNIK/langka
        (bukan kata generik yang dipunyai banyak tool sekaligus) dijamin masuk,
        TIDAK dibatasi top_k sama sekali -- persis kayak `_tools_wajib_selalu`.

        Layer 2 -- RECIPROCAL RANK FUSION (RRF) untuk sisa slot: menggabungkan
        ranking semantic & lexical (overlap nama+deskripsi, termasuk exact-match
        LEMAH) berdasarkan URUTAN posisi, bukan nilai skor mentah -- supaya aman
        walau distance metric ChromaDB di collection ini bukan cosine (lihat
        catatan di `_collection` -- tidak diset `hnsw:space: cosine` seperti di
        skill_lib.py). K_RRF dipakai kecil (bukan 60 seperti standar literatur
        buat web-search skala ribuan dokumen) -- candidate pool kita cuma
        belasan/puluhan tool, K besar bikin selisih antar-rank nyaris rata.

        Layer 3 -- Tool wajib (`_tools_wajib_selalu`) tetap dipaksa masuk paling
        akhir seperti sebelumnya, tidak berubah.

        MODE (`mode` param, atau default kelas `_default_retrieval_mode`):
          - "fusion" (DEFAULT BARU): semantic dari query ASLI + dokumen HyDE
            SEKALIGUS, di-RRF bareng (bobot_semantic dibagi rata ke keduanya).
            Kalau `llm` tidak dioper, otomatis setara "gorilla" murni (HyDE
            di-skip, bukan diam-diam dianggap "sudah jalan").
          - "hyde": HANYA dokumen hipotetis HyDE (exclusive, dipertahankan
            untuk debugging/A-B test, BUKAN untuk pemakaian produksi biasa).
          - "gorilla": HANYA query mentah (exclusive, sama alasan di atas).
          - "contextual": query mentah + prefix fase pentest saat ini.

        `rerank` (default False, OPT-IN): kalau True DAN `llm` dioper, slot
        sisa (di luar exact-match "kuat") diisi lewat LLM reranking atas pool
        kandidat RRF (lihat `_rerank_with_llm` -- prinsip #2 Contextual
        Retrieval, lever terbesar di paper Anthropic tapi tetap 1x panggilan
        LLM per giliran di retrieval-time, jadi tidak dijadikan default).
        Kalau True tapi `llm=None`, otomatis diabaikan (fallback ke RRF biasa,
        sama filosofi fallback seperti HyDE).
        """
        all_public_tools = cls.get_all_tools()

        # Fallback: Jika tidak ada kueri atau tools terlalu sedikit, kembalikan semua
        if not task_query or len(all_public_tools) <= top_k:
            return all_public_tools

        # --- PERUBAHAN MULTI-MODE: Gorilla vs HyDE vs Contextual vs Fusion ---
        retrieval_mode = (mode or cls._default_retrieval_mode).lower()

        tools_by_name = {t.name: t for t in all_public_tools}
        query_tokens = cls._tokenize(task_query)

        efektif_top_k = top_k
        if len(query_tokens) >= 12:
            efektif_top_k = min(top_k + 4, len(all_public_tools))
        elif len(query_tokens) >= 7:
            efektif_top_k = min(top_k + 2, len(all_public_tools))

        # --- LAPIS 1: KLASIFIKASI EXACT-MATCH KUAT vs LEMAH ---
        df_token = cls._hitung_df_token_nama(all_public_tools)
        nama_exact_kuat, nama_exact_lemah = set(), set()
        for t in all_public_tools:
            klasifikasi = cls._klasifikasi_exact_match(query_tokens, t, df_token, ambang_df, ambang_cakupan)
            if klasifikasi == "kuat":
                nama_exact_kuat.add(t.name)
            elif klasifikasi == "lemah":
                nama_exact_lemah.add(t.name)

        sumber_semantic = []  # list[(label, query_text)]
        if retrieval_mode == "fusion":
            sumber_semantic.append(("raw", task_query))
            hyde_doc = cls._generate_hyde_doc(task_query, llm=llm, all_tools=all_public_tools)
            if hyde_doc and hyde_doc.strip().lower() != task_query.strip().lower():
                sumber_semantic.append(("hyde", hyde_doc))
        elif retrieval_mode == "hyde":
            sumber_semantic.append(("hyde", cls._generate_hyde_doc(task_query, llm=llm, all_tools=all_public_tools)))
        elif retrieval_mode == "contextual":
            konteks_fase = f"Fase Pentest: {phase}. " if phase else ""
            sumber_semantic.append(("contextual", f"{konteks_fase}Tugas: {task_query}"))
        else:  # "gorilla" / raw
            sumber_semantic.append(("raw", task_query))

        jumlah_kandidat_semantic = min(efektif_top_k * 4, len(all_public_tools))
        bobot_per_sumber = bobot_semantic / len(sumber_semantic)
        ranking_semantic_gabungan = {}  # label -> ranking list, buat logging
        K_RRF = 5
        skor_rrf = defaultdict(float)
        for label, query_text in sumber_semantic:
            hasil = cls._collection.query(query_texts=[query_text], n_results=jumlah_kandidat_semantic)
            ranking = (hasil.get('ids') or [[]])[0]
            ranking_semantic_gabungan[label] = ranking
            for rank, nama in enumerate(ranking):
                skor_rrf[nama] += bobot_per_sumber * (1.0 / (K_RRF + rank + 1))

        # --- Lexical ranking (overlap nama+deskripsi+intents) ---
        skor_lexical_semua = [
            (t.name, cls._skor_lexical(query_tokens, t)) for t in all_public_tools
        ]
        ranking_lexical = [
            nama for nama, skor in sorted(skor_lexical_semua, key=lambda kv: kv[1], reverse=True)
            if skor > 0
        ]
        for rank, nama in enumerate(ranking_lexical):
            skor_rrf[nama] += (1 - bobot_semantic) * (1.0 / (K_RRF + rank + 1))
        terurut = sorted(skor_rrf.items(), key=lambda kv: kv[1], reverse=True)

        # --- GABUNGKAN: KUAT dulu (TANPA batas top_k), baru isi sisa slot dari RRF ---
        tools_terpilih = []
        nama_terpilih = set()
        for nama in sorted(nama_exact_kuat, key=lambda n: skor_rrf.get(n, 0.0), reverse=True):
            if nama in tools_by_name and nama not in nama_terpilih:
                tools_terpilih.append(tools_by_name[nama])
                nama_terpilih.add(nama)

        rerank_terpakai = False
        if rerank and llm is not None:
            # --- Contextual Retrieval prinsip #2: LLM rerank OPT-IN atas pool RRF ---
            # Exact-match "kuat" TIDAK ikut di-rerank (tetap dijamin masuk seperti
            # sebelumnya) -- rerank cuma memperbaiki URUTAN sisa slot dari pool
            # kandidat yang lebih besar dari RRF, supaya rerank punya bahan
            # pilihan yang cukup (bukan cuma reorder daftar yang sudah final).
            slot_tersisa = efektif_top_k - len(tools_terpilih)
            if slot_tersisa > 0:
                ukuran_pool = min(max(slot_tersisa * 3, slot_tersisa), len(all_public_tools))
                pool_kandidat = []
                for nama, _ in terurut:
                    if len(pool_kandidat) >= ukuran_pool:
                        break
                    if nama in nama_terpilih or nama not in tools_by_name:
                        continue
                    pool_kandidat.append(tools_by_name[nama])

                hasil_rerank = cls._rerank_with_llm(task_query, pool_kandidat, llm, slot_tersisa)
                for t in hasil_rerank:
                    if len(tools_terpilih) >= efektif_top_k:
                        break
                    if t.name in nama_terpilih:
                        continue
                    tools_terpilih.append(t)
                    nama_terpilih.add(t.name)
                rerank_terpakai = True
        elif rerank and llm is None:
            print(
                "⚠️ [Contextual-Rerank] rerank=True tapi `llm` TIDAK dioper ke "
                "get_relevant_tools() -- rerank otomatis diabaikan, fallback ke RRF biasa."
            )

        if not rerank_terpakai:
            for nama, _ in terurut:
                if len(tools_terpilih) >= efektif_top_k:
                    break
                if nama in nama_terpilih or nama not in tools_by_name:
                    continue
                tools_terpilih.append(tools_by_name[nama])
                nama_terpilih.add(nama)

        semantic_log = " | ".join(
            f"{label}_top={rank[:efektif_top_k]}" for label, rank in ranking_semantic_gabungan.items()
        )
        print(
            f"\n[🦍 Tool-RAG Mode: {retrieval_mode.upper()} | rerank={'ON' if rerank_terpakai else 'off'} | "
            f"top_k_efektif={efektif_top_k}] "
            f"exact_kuat={sorted(nama_exact_kuat)} | exact_lemah={sorted(nama_exact_lemah)} | "
            f"{semantic_log} | lexical_top={ranking_lexical[:efektif_top_k]} | "
            f"terpilih={[t.name for t in tools_terpilih]}"
        )

        # PENGAMANAN: Pastikan tool kontrol sistem WAJIB ikut, apa pun hasil RAG-nya
        nama_terpilih = {t.name for t in tools_terpilih}
        for t in all_public_tools:
            if t.name in cls._tools_wajib_selalu and t.name not in nama_terpilih:
                tools_terpilih.append(t)
                nama_terpilih.add(t.name)

        return tools_terpilih

class FailsafeRegistry:
    """
    Registry untuk skenario GAGAL/FAILSAFE di dalam graf (mis. AI balik dengan
    respons kosong berkali-kali). Polanya sengaja dibuat identik dengan
    ToolRegistry & ToolFormatterRegistry: kontributor cukup pasang decorator
    di file plugin masing-masing (folder `plugins/`, ke-scan otomatis oleh
    AUTO-DISCOVERY di agent_factory.py) -- TIDAK PERLU membuka atau mengubah
    core system (agent_nodes.py) sama sekali untuk mengganti perilaku failsafe.

    Setiap skenario diberi `kode` unik (mis. "kosong" untuk kasus respons AI
    kosong berulang). Kalau tidak ada handler custom terdaftar untuk kode itu
    -- atau handler-nya error -- sistem otomatis jatuh ke default bawaan yang
    dikirim oleh si pemanggil (node core), jadi node core TETAP JALAN NORMAL
    walau belum ada satupun plugin failsafe terpasang.

    Cara pakai di file plugin:

        from core_agent.registry import FailsafeRegistry

        @FailsafeRegistry.register("kosong")
        def pesan_kosong_versi_saya(state) -> str:
            return "Pesan custom kamu di sini, boleh baca `state` juga."

    Untuk kontrol penuh (bukan cuma ganti teks -- misal mau nambah field state
    lain, trigger notifikasi, dst), handler boleh return dict langsung; dict
    itu dipakai APA ADANYA sebagai update state LangGraph:

        @FailsafeRegistry.register("kosong")
        def handler_lanjutan(state) -> dict:
            return {"messages": [...], "revision_count": 0, "pending_tasks": ""}
    """
    _handlers = {}

    KODE_KOSONG = "kosong"

    @classmethod
    def register(cls, kode: str):
        """Decorator: daftarkan handler(state) -> str|dict untuk satu kode failsafe."""
        def decorator(func):
            cls._handlers[kode] = func
            return func
        return decorator

    @classmethod
    def get_update(cls, kode: str, state, default_pesan: str) -> dict:
        """
        Dipanggil dari node core. Mengembalikan dict update state siap pakai.
        - Tidak ada handler terdaftar utk `kode`  -> pakai default_pesan.
        - Handler terdaftar & return str          -> dibungkus jadi AIMessage.
        - Handler terdaftar & return dict          -> dipakai apa adanya (kontrol penuh).
        - Handler error / return kosong            -> fallback ke default_pesan
          (supaya plugin yang ditulis asal-asalan tidak menjatuhkan seluruh graf).
        """
        revision_count = state.get("revision_count", 0) if hasattr(state, "get") else 0
        default_update = {
            "messages": [AIMessage(content=default_pesan)],
            "revision_count": -revision_count,
        }

        handler = cls._handlers.get(kode)
        if handler is None:
            return default_update

        try:
            hasil = handler(state)
            if isinstance(hasil, dict):
                return hasil
            if isinstance(hasil, str) and hasil.strip():
                return {
                    "messages": [AIMessage(content=hasil)],
                    "revision_count": -revision_count,
                }
            return default_update
        except Exception as e:
            print(f"⚠️ [FailsafeRegistry] Handler custom untuk kode '{kode}' error, pakai default. Detail: {e}")
            return default_update

class GuardrailRegistry:
    """
    Registry untuk validasi ARGUMEN tool call SEBELUM tool-nya benar-benar
    dieksekusi -- didaftarkan PER KATEGORI (mis. "pentest"), bukan per tool
    satu-satu, supaya proteksi konsisten untuk semua tool dalam kategori yang
    sama tanpa perlu duplikasi validasi di tiap file tool.

    Pola sengaja dibuat identik dengan FailsafeRegistry & SmokeTestRegistry:
    kontributor cukup pasang decorator di file plugin masing-masing --
    TIDAK PERLU membuka atau mengubah core (agent_router.py/agent_nodes.py)
    untuk menambah/mengganti aturan validasi.

    Cara pakai di file plugin:

        from core_agent.registry import GuardrailRegistry

        @GuardrailRegistry.register("pentest")
        def validasi_pentest(nama_tool: str, args: dict) -> str | None:
            # return None kalau lolos, atau STRING ALASAN PENOLAKAN kalau ditolak.
            # String itu yang akan dikirim balik ke LLM sebagai ToolMessage,
            # menggantikan eksekusi tool yang sesungguhnya.
            if "DROP TABLE" in str(args).upper():
                return f"Tool '{nama_tool}' ditolak: argumen menyerupai payload SQLi."
            return None

    Kalau tidak ada handler terdaftar untuk sebuah kategori, semua tool call
    di kategori itu otomatis LOLOS tanpa validasi tambahan (opt-in per
    kategori -- kategori yang belum didaftarkan guardrail-nya tetap jalan
    normal seperti sebelumnya, tidak mengubah perilaku existing).

    PENTING: registry ini cuma menyimpan & memanggil fungsi validasi. Node
    LangGraph yang benar-benar mengeksekusi tool untuk kategori "pentest"
    (atau kategori lain yang mau divalidasi) harus memanggil
    `GuardrailRegistry.check(kategori, nama_tool, args)` untuk SETIAP
    tool_call SEBELUM menjalankan tool-nya, dan kalau hasilnya bukan None,
    kirim itu sebagai ToolMessage lalu SKIP eksekusi tool yang sesungguhnya.
    """
    _guardrails = {}

    @classmethod
    def register(cls, kategori: str):
        """Decorator: daftarkan validator(nama_tool, args) -> str|None untuk satu kategori."""
        def decorator(func):
            cls._guardrails[kategori] = func
            return func
        return decorator

    @classmethod
    def check(cls, kategori: str, nama_tool: str, args: dict) -> str | None:
        """
        Dipanggil dari node core sebelum eksekusi tool. Mengembalikan None
        kalau lolos (atau tidak ada guardrail terdaftar untuk kategori ini),
        atau string alasan penolakan kalau tool call ini harus diblokir.
        Error di dalam handler custom tidak menjatuhkan graf -- dianggap
        lolos dengan warning ke log (sama seperti filosofi FailsafeRegistry).
        """
        handler = cls._guardrails.get(kategori)
        if handler is None:
            return None
        try:
            return handler(nama_tool, args)
        except Exception as e:
            print(f"⚠️ [GuardrailRegistry] Handler validasi kategori '{kategori}' error, tool LOLOS default. Detail: {e}")
            return None


class SmokeTestRegistry:
    _tests = {}
 
    @classmethod
    def register(cls, nama_file: str):
        """Decorator: daftarkan fungsi tes(modul) -> None (lempar exception kalau gagal)."""
        def decorator(func):
            cls._tests[nama_file] = func
            return func
        return decorator
 
    @classmethod
    def get_test(cls, nama_file: str):
        """Ambil fungsi tes terdaftar untuk nama_file, atau None kalau belum ada."""
        return cls._tests.get(nama_file)