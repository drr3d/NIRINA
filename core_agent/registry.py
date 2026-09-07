import re
import hashlib
import time
from langchain_core.tools import tool
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
    _synthetic_intents = {
        "nmap_scan": "port scan, cek port terbuka, recon target, enum service, os detection, ping sweep, nmap",
        "sqlmap_scan": "dump database, bypass login, tes sqli, injeksi sql, ambil tabel user, vulnerability scan db",
        "gobuster_scan": "bruteforce directory, cari endpoint tersembunyi, enum URL, fuzzing path, temukan admin panel",
        "wpscan": "scan wordpress, cek plugin vulnerable, enum user wp, wordpress exploit",
        "metasploit_exploit": "eksekusi payload, dapatkan reverse shell, RCE, gain access, exploit vulnerability"
    }

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

        # --- PERBAIKAN: EARLY EXIT (SHORT-CIRCUIT) ---
        # Jika tool yang cocok secara "kuat" sudah memenuhi atau melebihi target top_k,
        # Skip HyDE dan semantic search untuk menghemat waktu.
        if len(nama_exact_kuat) >= efektif_top_k:
            print(f"[⚡ Short-Circuit] Ditemukan {len(nama_exact_kuat)} exact-match kuat. Melewati proses HyDE dan Semantic RAG.")
            tools_terpilih = [tools_by_name[n] for n in list(nama_exact_kuat)[:efektif_top_k]]
            
            # PENGAMANAN: Tetap paksa masukkan tool wajib
            for t in all_public_tools:
                if t.name in cls._tools_wajib_selalu and t.name not in nama_exact_kuat:
                    tools_terpilih.append(t)
            
            return tools_terpilih

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