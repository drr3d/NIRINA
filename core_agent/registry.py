import re
from langchain_core.tools import tool
from langchain_core.messages import AIMessage
from collections import defaultdict

import chromadb
from chromadb.utils import embedding_functions

class ToolRegistry:
    """Registry framework dinamis dengan Backward Compatibility penuh + Tool-RAG."""
    _tools = defaultdict(list)
    _internal_tools = {}

    # --- TAMBAHAN UNTUK TOOL-RAG (GORILLA STYLE) ---
    _chroma_client = chromadb.PersistentClient(path="./chroma_db_nirina")
    _embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="paraphrase-multilingual-MiniLM-L12-v2")
    _collection = _chroma_client.get_or_create_collection(name="tool_library", embedding_function=_embed_fn)

    _tools_wajib_selalu = {"tools_reward", "tools_gagal", "tools_batal", "lupakan_skill_gagal"}
    # -----------------------------------------------

    @classmethod
    def daftar_tool_wajib(cls, *nama_tool: str):
        """Tandai satu/lebih nama tool supaya SELALU ikut di get_relevant_tools,
        berapapun top_k-nya dan apapun hasil semantic search-nya. Dipanggil dari
        agent_factory.py (atau file plugin) saat startup -- bukan hardcode di
        sini -- supaya menambah tool kontrol baru tidak perlu sentuh registry.py."""
        cls._tools_wajib_selalu.update(nama_tool)

    @classmethod
    def register(cls, is_sensitive: bool = None, category: str = None, is_internal: bool = False):
        # ... (KODE ANDA TETAP SAMA PERSIS) ...
        if category is None:
            target_category = "sensitive" if is_sensitive else "safe"
        else:
            target_category = category

        def decorator(func):
            langchain_tool = tool(func)
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
    # FITUR BARU: SINKRONISASI & PENCARIAN ALAT (AMAN & TERPISAH)
    # =========================================================
    @classmethod
    def sync_tools_to_db(cls):
        """Sinkronisasi deskripsi tool publik ke ChromaDB saat startup."""
        ids = []
        documents = []
        
        # Ekstrak dari objek langchain_tool (yang punya atribut .name dan .description)
        for t in cls.get_all_tools():
            ids.append(t.name)
            # Rangkai nama dan deskripsi untuk jadi embedding pencarian
            documents.append(f"Nama Tool: {t.name}\nDeskripsi: {t.description}")
        
        if ids:
            cls._collection.upsert(documents=documents, ids=ids)
            print(f"[Tool-RAG] {len(ids)} tools berhasil disinkronisasi ke memori.")

    @staticmethod
    def _tokenize(teks: str) -> set:
        """Tokenisasi kasar (lowercase, alfanumerik) -- bukan NLP canggih, cuma
        buat exact/substring token match di skor lexical di bawah.
        [FIX] Regex SENGAJA tidak menyertakan underscore sebagai bagian token
        (beda dari versi awal) -- nama tool ditulis snake_case (mis. "nmap_scan",
        "sqlmap_scan"), dan kalau underscore dianggap bagian token, nama itu jadi
        SATU token utuh "nmap_scan" yang TIDAK PERNAH exact-match ke kata tunggal
        "nmap" di query user. Meng-split di underscore bikin "nmap_scan" pecah
        jadi {"nmap","scan"} -- exact-match ke kata tunggal jadi berfungsi."""
        return set(re.findall(r"[a-z0-9]+", (teks or "").lower()))

    @classmethod
    def _hitung_df_token_nama(cls, all_tools) -> dict:
        """
        Document frequency token -> di berapa BANYAK nama tool token itu
        muncul. Dipakai buat bedain kata GENERIK (mis. "buat", "scan", "file" --
        nongol di banyak nama tool sekaligus) vs kata SPESIFIK/LANGKA (mis.
        "openrouter", "baca", "wapiti" -- cuma milik 1 tool doang). Ini yang
        bikin exact-match "adil": exact-match ke kata generik TIDAK dianggap
        sekuat exact-match ke kata yang benar-benar unik.
        """
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
        tool_tokens = cls._tokenize(f"{tool.name} {tool.description or ''}")
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
            
        # 1. Syarat Cakupan Mayoritas (Sangat ketat, 80% token nama tool harus ada di kueri)
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
            # Jika nama tool panjang (3 kata atau lebih), butuh setidaknya 2 token overlap agar tidak salah tangkap
            elif len(overlap_nama) >= 2:
                return "kuat"
                
        # Jika gagal melewati syarat ketat di atas, turunkan kasta menjadi "lemah"
        return "lemah"

    @classmethod
    def get_relevant_tools(cls, task_query: str, top_k: int = 3, bobot_semantic: float = 0.65):
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
        """
        all_public_tools = cls.get_all_tools()

        # Fallback: Jika tidak ada kueri atau tools terlalu sedikit, kembalikan semua
        if not task_query or len(all_public_tools) <= top_k:
            return all_public_tools

        tools_by_name = {t.name: t for t in all_public_tools}
        query_tokens = cls._tokenize(task_query)

        # --- LAPIS 1: KLASIFIKASI EXACT-MATCH KUAT vs LEMAH ---
        df_token = cls._hitung_df_token_nama(all_public_tools)
        nama_exact_kuat, nama_exact_lemah = set(), set()
        for t in all_public_tools:
            klasifikasi = cls._klasifikasi_exact_match(query_tokens, t, df_token)
            if klasifikasi == "kuat":
                nama_exact_kuat.add(t.name)
            elif klasifikasi == "lemah":
                nama_exact_lemah.add(t.name)

        # --- Semantic ranking (urutan dari ChromaDB, bukan nilai distance mentahnya) ---
        jumlah_kandidat_semantic = min(top_k * 4, len(all_public_tools))
        hasil = cls._collection.query(query_texts=[task_query], n_results=jumlah_kandidat_semantic)
        ranking_semantic = (hasil.get('ids') or [[]])[0]

        # --- Lexical ranking (overlap nama+deskripsi, superset dari exact-match) ---
        skor_lexical_semua = [
            (t.name, cls._skor_lexical(query_tokens, t)) for t in all_public_tools
        ]
        ranking_lexical = [
            nama for nama, skor in sorted(skor_lexical_semua, key=lambda kv: kv[1], reverse=True)
            if skor > 0
        ]

        # --- LAPIS 2: RRF untuk sisa slot (di luar exact-match KUAT) ---
        K_RRF = 5
        skor_rrf = defaultdict(float)
        for rank, nama in enumerate(ranking_semantic):
            skor_rrf[nama] += bobot_semantic * (1.0 / (K_RRF + rank + 1))
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
        for nama, _ in terurut:
            if len(tools_terpilih) >= top_k:
                break
            if nama in nama_terpilih or nama not in tools_by_name:
                continue
            tools_terpilih.append(tools_by_name[nama])
            nama_terpilih.add(nama)

        print(
            f"\n[🦍 Tool-RAG Hybrid] exact_kuat={sorted(nama_exact_kuat)} | "
            f"exact_lemah={sorted(nama_exact_lemah)} | "
            f"semantic_top={ranking_semantic[:top_k]} | lexical_top={ranking_lexical[:top_k]} | "
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

    # Kode bawaan yang dikenali graf inti. Kontributor bebas mendaftarkan kode
    # baru sendiri (string apa saja) kalau suatu saat menambah node failsafe
    # lain -- tidak wajib didaftarkan di sini, ini cuma referensi baku biar
    # tidak typo saat register/pemanggilan.
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
    """
    Registry buat smoke test otomatis yang dijalankan tulis_file SEBELUM
    revisi kode dari agent benar-benar disimpan permanen ke disk. Beda dari
    FailsafeRegistry (urusannya respons LLM yang gagal), ini urusannya
    validasi KODE HASIL TULISAN LLM -- lapisan pertahanan tambahan karena
    model kecil bisa nulis kode yang sintaksnya benar tapi salah runtime
    (argumen kurang, API ketuker, dsb) dan itu cuma ketahuan begitu benar-benar
    dieksekusi.
 
    Kontributor daftar smoke test PER NAMA FILE (bukan per tool) di file
    plugin masing-masing:
 
        from core_agent.registry import SmokeTestRegistry
 
        @SmokeTestRegistry.register("sqllogin.py")
        def tes_sqllogin(modul):
            # `modul` = module Python hasil importlib.reload() dari file yang
            # BARU ditulis (belum permanen -- tulis_file akan rollback kalau
            # test ini gagal). Lempar Exception/assert apapun kalau gagal.
            hasil = modul.verifikasi_login("admin", "rahasia123")
            assert "LOGIN_SUKSES" in hasil, f"Login normal harus tetap sukses: {hasil}"
 
    Kalau tidak ada test terdaftar untuk sebuah file, tulis_file tetap jalan
    normal tanpa validasi tambahan (opt-in per file, tidak mengubah perilaku
    file yang belum ada test-nya).
    """
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