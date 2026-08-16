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
    _embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    _collection = _chroma_client.get_or_create_collection(name="tool_library", embedding_function=_embed_fn)

    # [BARU] Tool yang WAJIB selalu ikut ke LLM apapun hasil semantic search-nya
    # (kontrol-alur orkestrasi, bukan tool "task" -- jadi similarity-nya ke
    # deskripsi task user seringkali rendah padahal harus tetap tersedia tiap
    # giliran, mis. tools_reward/tools_gagal/tools_batal utk nutup skill trace,
    # atau delegasi_koder/konsultasi_planner/tulis_file yg didaftarkan dari
    # agent_factory.py saat startup). Sengaja dibuat mutable + method extend
    # supaya plugin/agent_factory bisa nambah tanpa perlu edit file ini lagi.
    _tools_wajib_selalu = {"tools_reward", "tools_gagal", "tools_batal"}
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
    def _skor_lexical(cls, query_tokens: set, tool) -> float:
        """[BARU] Skor keyword/lexical sederhana (token overlap, bukan BM25 penuh --
        sengaja tanpa dependency tambahan) antara query & (nama+deskripsi) tool.

        KENAPA PERLU, PADAHAL SUDAH ADA SEMANTIC SEARCH:
        Embedding model kecil (all-MiniLM-L6-v2) bagus buat kemiripan MAKNA umum,
        tapi sering false-negative untuk istilah teknis SPESIFIK di tools pentest --
        mis. query "scan port 8080 pake nmap" vs tool bernama "eksekusi_cmd_windows"
        bisa punya cosine similarity tinggi (sama-sama "eksekusi teknis") padahal
        tool yang BENAR (mis. "nmap_scan") justru kalah similarity-nya kalau
        deskripsinya ditulis kurang deskriptif. Exact match token "nmap" di nama
        tool adalah sinyal kuat yang sering dilewatkan murni oleh semantic search.
        Skor dinormalisasi ke overlap/len(query_tokens) supaya query panjang tidak
        otomatis unggul dibanding query pendek yang presisi.
        """
        if not query_tokens:
            return 0.0
        tool_tokens = cls._tokenize(f"{tool.name} {tool.description or ''}")
        if not tool_tokens:
            return 0.0
        overlap = query_tokens & tool_tokens
        if not overlap:
            return 0.0
        # Bonus kalau overlap-nya ada di NAMA tool (bukan cuma deskripsi) --
        # exact match nama tool (mis. "nmap" nyantol ke "nmap_scan") jauh lebih
        # kuat sebagai sinyal daripada nyantol di kalimat deskripsi yang panjang.
        nama_tokens = cls._tokenize(tool.name)
        bonus_nama = 0.5 if (query_tokens & nama_tokens) else 0.0
        return (len(overlap) / len(query_tokens)) + bonus_nama

    @classmethod
    def get_relevant_tools(cls, task_query: str, top_k: int = 3, bobot_semantic: float = 0.65):
        """[HYBRID] Filter dinamis tool sebagai input LLM -- gabungan semantic
        search (ChromaDB embedding, nangkep kemiripan MAKNA) + lexical/keyword
        exact-match (nangkep istilah teknis SPESIFIK yang sering dilewatkan
        embedding model kecil), digabung lewat 2 lapis:

        Layer 1 -- PROMOSI KERAS untuk EXACT-NAME MATCH: kalau ada token di
        query yang match PERSIS ke token di NAMA tool (mis. "nmap" di query vs
        tool "nmap_scan"), tool itu dijamin masuk prioritas TERATAS, TIDAK
        digantungkan ke bobot_semantic. Ini WAJIB dibuat promosi keras, bukan
        cuma penjumlahan skor tertimbang -- soalnya kalau exact-match cuma jadi
        salah satu kontributor skor RRF, dan bobot_semantic > 0.5 (default di
        sini 0.65), tool yang menang telak di semantic-tapi-salah-arah akan
        SELALU mengalahkan exact-match di lexical (sudah dibuktikan lewat test:
        query "scan pakai nmap" vs tool "eksekusi_cmd_windows" menang duluan di
        semantic rank-0, ngalahin "nmap_scan" yang exact-match namanya, kalau
        cuma dijumlah-tertimbang). Makanya exact-name match harus dipromosikan
        DI LUAR skema pembobotan itu.

        Layer 2 -- RECIPROCAL RANK FUSION (RRF) untuk sisa slot: menggabungkan
        ranking semantic & lexical (overlap nama+deskripsi, bukan cuma exact-
        name) berdasarkan URUTAN posisi, bukan nilai skor mentah -- supaya aman
        walau distance metric ChromaDB di collection ini bukan cosine (lihat
        catatan di `_collection` -- tidak diset `hnsw:space: cosine` seperti di
        skill_lib.py, jadi nilai distance mentahnya TIDAK bisa langsung
        diperlakukan sebagai "1 - similarity"). K_RRF dipakai kecil (bukan 60
        seperti standar literatur buat web-search skala ribuan dokumen) --
        candidate pool kita cuma belasan/puluhan tool, K besar bikin selisih
        antar-rank nyaris rata dan kehilangan daya beda di pool sekecil ini.
        """
        all_public_tools = cls.get_all_tools()

        # Fallback: Jika tidak ada kueri atau tools terlalu sedikit, kembalikan semua
        if not task_query or len(all_public_tools) <= top_k:
            return all_public_tools

        tools_by_name = {t.name: t for t in all_public_tools}
        query_tokens = cls._tokenize(task_query)

        # --- LAPIS 1: EXACT-NAME MATCH (promosi keras) ---
        nama_exact_match = {t.name for t in all_public_tools if query_tokens & cls._tokenize(t.name)}

        # --- Semantic ranking (urutan dari ChromaDB, bukan nilai distance mentahnya) ---
        jumlah_kandidat_semantic = min(top_k * 4, len(all_public_tools))
        hasil = cls._collection.query(query_texts=[task_query], n_results=jumlah_kandidat_semantic)
        ranking_semantic = (hasil.get('ids') or [[]])[0]

        # --- Lexical ranking (overlap nama+deskripsi, superset dari exact-name match) ---
        skor_lexical_semua = [
            (t.name, cls._skor_lexical(query_tokens, t)) for t in all_public_tools
        ]
        ranking_lexical = [
            nama for nama, skor in sorted(skor_lexical_semua, key=lambda kv: kv[1], reverse=True)
            if skor > 0
        ]

        # --- LAPIS 2: RRF untuk sisa slot ---
        K_RRF = 5
        skor_rrf = defaultdict(float)
        for rank, nama in enumerate(ranking_semantic):
            skor_rrf[nama] += bobot_semantic * (1.0 / (K_RRF + rank + 1))
        for rank, nama in enumerate(ranking_lexical):
            skor_rrf[nama] += (1 - bobot_semantic) * (1.0 / (K_RRF + rank + 1))

        terurut = sorted(skor_rrf.items(), key=lambda kv: kv[1], reverse=True)
        # Promosikan exact-name match ke depan antrian (diurutkan sesama mereka
        # pakai skor RRF-nya juga, bukan asal urutan set), baru isi sisa slot
        # dari hasil RRF biasa.
        terurut_prioritas = (
            [(nama, skor) for nama, skor in terurut if nama in nama_exact_match]
            + [(nama, skor) for nama, skor in terurut if nama not in nama_exact_match]
        )
        tools_terpilih = [
            tools_by_name[nama] for nama, _ in terurut_prioritas[:top_k] if nama in tools_by_name
        ]

        print(
            f"\n[🦍 Tool-RAG Hybrid] exact_match={sorted(nama_exact_match)} | "
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