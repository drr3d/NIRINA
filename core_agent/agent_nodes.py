import operator
import json
import hashlib
import random
from typing import Annotated, TypedDict, Any, Optional

# Import LangChain & LangGraph components
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import add_messages

# ==========================================
# --- HELPER: SIGNATURE TOOL CALL ---
# ==========================================
def _signature_tool_calls(tool_calls: list) -> str:
    """
    Bikin signature stabil (hash pendek) dari daftar tool_calls berdasarkan
    nama + argumen -- dipakai AIBrainProcessor untuk mendeteksi apakah AI
    mengulang pemanggilan tool yang PERSIS SAMA berturut-turut (lihat
    tool_repeat_count/last_tool_signature di AgentState, dan guard
    MAX_TOOL_REPEAT di agent_router.py).

    Diurutkan (sorted) supaya kalau ada parallel tool calls, urutan
    kemunculannya tidak mempengaruhi hasil signature.
    """
    if not tool_calls:
        return ""
    normalisasi = sorted(
        (
            tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", ""),
            json.dumps(
                tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {}),
                sort_keys=True,
                default=str,
            ),
        )
        for tc in tool_calls
    )
    raw = json.dumps(normalisasi)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

# ==========================================
# --- 0. FUNGSI SUMMARIZER (LLM KECIL) ---
# ==========================================
def buat_ringkasan_memori(pesan_lama: list, fast_llm: Any, ringkasan_sebelumnya: str = "") -> str:
    """Menggunakan LLM sekunder yang cepat untuk meringkas obrolan usang."""
    teks_obrolan = ""
    for p in pesan_lama:
        # [FIX] Sebelumnya cuma bedain "User" (HumanMessage) vs "AI" (semua yang
        # lain) -- ToolMessage ikut kelabel "AI", padahal isinya hasil tool
        # (mis. output ZAP/SQLMap), bukan omongan AI. Sekarang dilabel eksplisit
        # pakai nama tool-nya supaya fast_llm tahu ini data mentah dari tool,
        # bukan narasi AI, dan bisa meringkasnya secara akurat.
        if p.type == "human":
            peran = "User"
        elif p.type == "tool":
            peran = f"Hasil Tool[{getattr(p, 'name', '?')}]"
        else:
            peran = "AI"
        if p.content: # Kadang AI manggil tool tanpa teks, kita ambil teksnya saja
            teks_obrolan += f"{peran}: {p.content}\n"
            
    # Jika tidak ada teks untuk diringkas (misal cuma tool call kosong), lewati
    if not teks_obrolan.strip():
        return ringkasan_sebelumnya

    prompt = ChatPromptTemplate.from_messages([
        ("system", 
         "Kamu adalah asisten memori internal AI. Tugasmu meringkas percakapan lama. "
         "Pertahankan instruksi teknis, fakta, atau keputusan penting. Untuk baris "
         "'Hasil Tool[...]', catat temuan konkretnya SECARA SPESIFIK dan akurat "
         "(mis. endpoint yang ditemukan, parameter rentan, kredensial, pesan error) -- "
         "JANGAN digeneralisir jadi kalimat samar seperti 'tool berhasil dijalankan'. "
         "Gabungkan dengan ringkasan sebelumnya secara mulus.\n\n"
         "Ringkasan Sebelumnya:\n{ringkasan_sebelumnya}"
        ),
        ("user", "Rangkum obrolan berikut:\n\n{obrolan}")
    ])
    
    # Langsung jalankan chain
    hasil = (prompt | fast_llm).invoke({"ringkasan_sebelumnya": ringkasan_sebelumnya, "obrolan": teks_obrolan})
    return hasil.content

def _panjang_args_tool_calls(tool_calls) -> int:
    """Total panjang (karakter) semua argumen tool_calls, dalam bentuk JSON. Dipakai
    untuk cek ambang kompresi & buat katup ukuran dalam-giliran (lihat di bawah)."""
    total = 0
    for tc in (tool_calls or []):
        args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
        total += len(json.dumps(args, default=str))
    return total


def _kompres_tool_calls(tool_calls, ambang: int):
    """Ganti NILAI argumen tool_calls yang kepanjangan (mis. isi file/kode lengkap
    yang dikirim ke tool 'tulis_file') dengan placeholder pendek. `id`/`name` tool_call
    SELALU dipertahankan utuh -- itu yang dipakai LangChain/Ollama buat memasangkan
    AIMessage ini dengan ToolMessage balasannya, jadi TIDAK BOLEH ikut berubah."""
    hasil = []
    for tc in (tool_calls or []):
        is_dict = isinstance(tc, dict)
        nama = tc.get("name") if is_dict else getattr(tc, "name", "")
        tc_id = tc.get("id") if is_dict else getattr(tc, "id", None)
        args = tc.get("args") if is_dict else getattr(tc, "args", {})
        args_str = json.dumps(args, default=str)
        args_final = (
            {"_dikompres": f"Argumen asli '{nama}' dipangkas ({len(args_str)} char). Panggil ulang tool-nya kalau butuh detail lengkap."}
            if len(args_str) > ambang else args
        )
        hasil.append({"name": nama, "args": args_final, "id": tc_id, "type": "tool_call"})
    return hasil


def optimasi_konteks_langchain(
    messages,
    current_summary="",
    fast_llm=None,
    batas_pesan_inturn: int = 15,
    batas_karakter_inturn: int = 20_000,
    panjang_min_kompresi: int = 300,
):
    """
    Optimasi berbasis 'Batas Giliran' (Turn Boundary) -- pengganti sliding-window
    lama yang berbasis jarak-dari-ujung list.

    """
    from langchain_core.messages import ToolMessage, AIMessage

    BATAS_PESAN_AMAN_DALAM_GILIRAN = batas_pesan_inturn  # default 15 -- Up from 8, if you've more vram on you gpu you, can increase this
    BATAS_KARAKTER_AMAN_DALAM_GILIRAN = batas_karakter_inturn  # default 20_000 -- kaitannya ke num_ctx: lihat penjelasan di __init__ AIBrainProcessor
    PANJANG_MIN_UNTUK_KOMPRESI = panjang_min_kompresi  # default 300 -- dipakai bareng utk ToolMessage.content DAN tool_calls args

    # 1. Cari index HumanMessage TERAKHIR -> penanda mulainya giliran aktif.
    #    Pesan di idx < last_human_idx berarti berasal dari giliran yg sudah selesai.
    last_human_idx = 0
    for idx, msg in enumerate(messages):
        if msg.type == "human":
            last_human_idx = idx

    total_msgs = len(messages)
    cleaned_messages = []
    pesan_untuk_diringkas = []

    akumulasi_karakter_inturn = 0

    N_EXEMPT_TOOL_TERBARU = 2
    tool_idx_inturn = [
        idx for idx, msg in enumerate(messages)
        if idx >= last_human_idx and msg.type == "tool"
    ]
    ai_toolcall_idx_inturn = [
        idx for idx, msg in enumerate(messages)
        if idx >= last_human_idx and msg.type == "ai" and getattr(msg, "tool_calls", None)
    ]
    idx_exempt_tool = set(tool_idx_inturn[-N_EXEMPT_TOOL_TERBARU:])
    idx_exempt_ai_toolcall = set(ai_toolcall_idx_inturn[-N_EXEMPT_TOOL_TERBARU:])

    for idx, msg in enumerate(messages):
        if msg.type == "system":
            cleaned_messages.append(msg)
            continue

        is_giliran_selesai = idx < last_human_idx

        # [FIX] Cek ambang pakai akumulasi SEBELUM pesan ini ditambahkan.
        long_context_inturn = not is_giliran_selesai and (
            (total_msgs - idx) > BATAS_PESAN_AMAN_DALAM_GILIRAN
            or akumulasi_karakter_inturn > BATAS_KARAKTER_AMAN_DALAM_GILIRAN 
        )

        if not is_giliran_selesai:
            akumulasi_karakter_inturn += len(msg.content or "")
            if getattr(msg, "tool_calls", None):
                akumulasi_karakter_inturn += _panjang_args_tool_calls(msg.tool_calls)

        # --- A. LOGIKA TOOL MESSAGE (Utuh dari versi Anda) ---
        if msg.type == "tool":
            dikecualikan = idx in idx_exempt_tool
            harus_dikompres = (
                (is_giliran_selesai or long_context_inturn)
                and len(msg.content) > PANJANG_MIN_UNTUK_KOMPRESI
                and not dikecualikan
            )

            if harus_dikompres:
                if fast_llm:
                    pesan_untuk_diringkas.append(msg)
                    status_ringkas = "sudah diringkas ke ingatan jangka panjang (lihat pesan '--- INGATAN JANGKA PANJANG AI ---' di atas)"
                else:
                    status_ringkas = "dipangkas (fast_llm tidak aktif, tidak ada yang meringkas isinya)"

                cleaned_messages.append(ToolMessage(
                    content=(
                        f"[Log memori: '{msg.name}' SUDAH SELESAI dieksekusi "
                        f"({len(msg.content)} char hasil asli) -- {status_ringkas}. "
                        "Tool ini SUDAH mengembalikan data -- JANGAN panggil "
                        "ulang tool yang sama kecuali kamu butuh detail baru "
                        "yang berbeda dari yang sudah didapat.]"
                    ),
                    name=msg.name,
                    tool_call_id=msg.tool_call_id
                ))
            else:
                cleaned_messages.append(msg)
            continue

        # --- B. LOGIKA HUMAN/AI MESSAGE (Dibersihkan & Diringkas) ---
        if is_giliran_selesai:
            if msg.type == "human":
                if fast_llm:
                    # Pesan User masuk ringkasan dan DIHAPUS dari HD memory
                    pesan_untuk_diringkas.append(msg)
                else:
                    # Tanpa fast_llm tidak ada yang bisa meringkas -> biarkan utuh
                    # supaya pesan tidak hilang begitu saja dari konteks.
                    cleaned_messages.append(msg)

            elif msg.type == "ai":
                if getattr(msg, "tool_calls", None):
                    # ⚠️ CRITICAL: Jika AI memanggil tool, JANGAN DIHAPUS dari HD memory!
                    # LangChain butuh pesan ini untuk validasi pasangan ToolMessage.
                    if fast_llm:
                        pesan_untuk_diringkas.append(msg)

                    if _panjang_args_tool_calls(msg.tool_calls) > PANJANG_MIN_UNTUK_KOMPRESI:
                        cleaned_messages.append(AIMessage(
                            content=msg.content,
                            tool_calls=_kompres_tool_calls(msg.tool_calls, PANJANG_MIN_UNTUK_KOMPRESI),
                            id=msg.id,
                        ))
                    else:
                        cleaned_messages.append(msg)
                else:
                    if fast_llm:
                        # Teks AI biasa masuk ringkasan dan DIHAPUS dari HD memory
                        pesan_untuk_diringkas.append(msg)
                    else:
                        # Tanpa fast_llm, biarkan utuh (tidak ada yang bisa meringkas)
                        cleaned_messages.append(msg)
        else:
            if (
                long_context_inturn
                and idx not in idx_exempt_ai_toolcall
                and getattr(msg, "tool_calls", None)
                and _panjang_args_tool_calls(msg.tool_calls) > PANJANG_MIN_UNTUK_KOMPRESI
            ):
                cleaned_messages.append(AIMessage(
                    content=msg.content,
                    tool_calls=_kompres_tool_calls(msg.tool_calls, PANJANG_MIN_UNTUK_KOMPRESI),
                    id=msg.id,
                ))
            else:
                cleaned_messages.append(msg)

    # --- C. EKSEKUSI LLM KECIL ---
    ringkasan_baru = current_summary
    if pesan_untuk_diringkas and fast_llm:
        print("\n[🧠 Memory Manager] Mengompresi masa lalu menggunakan Fast LLM...")
        ringkasan_baru = buat_ringkasan_memori(pesan_untuk_diringkas, fast_llm, current_summary)

    # --- D. INJEKSI KE STATE ---
    if ringkasan_baru:
        pesan_ingatan = SystemMessage(
            content=f"--- INGATAN JANGKA PANJANG AI ---\n{ringkasan_baru}\n---------------------------------"
        )
        # [KV-CACHE TRICK]: Selalu sisipkan di index 1!
        # Index 0 harus selalu base_prompt murni agar KV-Cache Ollama tidak hancur.
        if len(cleaned_messages) > 0 and cleaned_messages[0].type == "system":
            cleaned_messages.insert(1, pesan_ingatan)
        else:
            cleaned_messages.insert(0, pesan_ingatan)

    return cleaned_messages, ringkasan_baru

# ==========================================
# --- 1. ARSITEKTUR CUSTOM STATEGRAPH ---
# ==========================================
def replace_atau_tambah(existing: list, new) -> list:
    if new is None:
        return []          # None = sinyal reset
    return existing + new  # list = nambah

class AgentState(TypedDict):
    """
    Representasi memori sentral untuk AI Agent.
    - messages: Menyimpan riwayat obrolan (ditumpuk).
    - revision_count: Menghitung berapa kali AI sudah direvisi.
    """
    messages: Annotated[list, add_messages]
    revision_count: Annotated[int, operator.add]
    pending_tasks: str # <-- Tambahan baru, untuk monitoring pending task
    summary: str # <-- TAMBAHAN BARU: Wadah untuk ringkasan
    # --- Guard pengulangan tool call (lihat _signature_tool_calls di atas) ---
    last_tool_signature: str  # hash nama+args tool call terakhir (utk deteksi ulang persis)
    last_tool_names: str      # versi manusiawi (nama tool doang) buat reminder/log
    tool_repeat_count: Annotated[int, operator.add]  # berapa kali berturut-turut identik

     # ---Skill Library (Voyager-style) ---
    current_task_desc: str                              # diisi user saat kasih task baru (dipotong [:300], khusus skill library)
    current_task_desc_full: str                          # versi UTUH (tidak dipotong), khusus query Tool-RAG
    mode_eksplorasi: Optional[bool]                       # diputuskan SEKALI di awal task -- True = referensi skill sukses SENGAJA disembunyikan (dorong eksplorasi jalur baru)
    current_skill_trace: Annotated[list,  replace_atau_tambah]   # numpuk selama task berjalan
# ==========================================
# --- 2. DEFINISI NODE (KOMPONEN AI) ---
# ==========================================
class AIBrainProcessor:
    """
    Komponen Otak Utama (Brain Node) untuk AI Agent.
    """
    
    def __init__(
        self,
        llm_model: Any,
        tools_list: list,
        base_prompt: str,
        fast_llm: Any = None,
        enable_optimization: bool = True,
        batas_pesan_inturn: int = 15,
        batas_karakter_inturn: int = 20_000,
        panjang_min_kompresi: int = 300,

        skill_library: Any = None,   # <-- BARU: instance SkillLibrary, opsional
        top_k_skill: int = 3,

        maks_umur_skill_gagal_detik: Optional[float] = 2 * 24 * 3600,

        min_similarity_skill_sukses: float = 0.80,
        min_similarity_skill_gagal: float = 0.65,

        # --- GORILLA-STYLE DYNAMIC TOOL RETRIEVAL ---
        tool_registry: Any = None,   # <-- instance/class ToolRegistry, opsional
        top_k_tools: int = 8,
    ):
        """
        batas_karakter_inturn: ambang katup-ukuran di optimasi_konteks_langchain
        (lihat fungsi itu). Ini idealnya dihitung dari num_ctx model, BUKAN angka
        tetap -- soalnya dia mewakili "berapa karakter riwayat obrolan yang masih
        aman", dan itu jelas beda kalau num_ctx-nya beda. Kasarnya:
            num_ctx (token) x ~4 karakter/token = total kapasitas karakter model
        lalu sisain porsi besar buat system prompt + ringkasan memori + jatah
        model nulis jawaban -- makanya ambangnya cuma diambil sebagian (mis.
        ~25%) dari total itu, bukan semuanya. Contoh cara hitungnya ada di
        agent_factory.py (dekat definisi num_ctx model). Default 20_000 di sini
        cocok kira-kira buat num_ctx sekitar 20rb token -- kalau num_ctx-nya
        beda jauh, isi argumen ini saat bikin AIBrainProcessor, jangan ubah
        angka di dalam optimasi_konteks_langchain.

        panjang_min_kompresi: BEDA cerita -- ini gak dihitung dari num_ctx,
        cuma ambang "biar hasil kompresi beneran hemat" (placeholder-nya sendiri
        ~100-150 karakter, jadi ngompres pesan yang lebih pendek dari itu malah
        bikin lebih boros, bukan hemat). Longgar-longgar aja diikutin default.
        """
        self.base_prompt = base_prompt
        self.fast_llm = fast_llm

        # --- GORILLA-STYLE DYNAMIC TOOL RETRIEVAL ---
        self.tool_registry = tool_registry
        self.top_k_tools = top_k_tools
        self._tools_fallback = tools_list  # dipakai kalau Tool-RAG nonaktif ATAU query kosong
        self._llm_mentah = llm_model

        self.gorilla_aktif = (tool_registry is not None)
        self.enable_optimization = enable_optimization # <-- SAKELAR TOGGLE
        self.batas_pesan_inturn = batas_pesan_inturn
        self.batas_karakter_inturn = batas_karakter_inturn
        self.panjang_min_kompresi = panjang_min_kompresi

        self.skill_library = skill_library
        self.top_k_skill = top_k_skill
        self.maks_umur_skill_gagal_detik = maks_umur_skill_gagal_detik
        self.min_similarity_skill_sukses = min_similarity_skill_sukses
        self.min_similarity_skill_gagal = min_similarity_skill_gagal

    def set_gorilla_tool_rag(self, aktif: bool) -> str:
        """
        Nyalakan/matikan mekanisme Tool-RAG Gorilla-style secara
        RUNTIME (tanpa restart proses) -- dipanggil dari tool kontrol
        `atur_gorilla_tool_rag` (lihat plugin_atur_gorilla_tool_rag.py) yang
        bisa di-trigger langsung dari chat user. Berlaku mulai giliran
        BERIKUTNYA (giliran yang sedang berjalan saat tool ini dipanggil
        sudah terlanjur pakai keputusan lama).
        """
        if self.tool_registry is None:
            return (
                "Tool-RAG Gorilla tidak tersedia di deployment ini -- "
                "tool_registry tidak di-set saat AIBrainProcessor dibuat "
                "(lihat agent_factory.py), jadi tidak ada yang bisa dinyalakan."
            )
        self.gorilla_aktif = bool(aktif)
        status = "DIAKTIFKAN" if self.gorilla_aktif else "DINONAKTIFKAN"
        print(f"\n[⚙️ Runtime Toggle] Tool-RAG Gorilla {status} lewat perintah chat.")
        return f"Tool-RAG Gorilla berhasil {status}. Berlaku mulai giliran berikutnya."

    def _build_pending_reminder(self, pending_tasks: str) -> SystemMessage:
        """
        [OPTIMASI KV-CACHE] Dulu teks ini disambung ke system prompt (messages[0]),
        sehingga messages[0] berubah tiap giliran begitu pending_tasks berubah -> prefix
        prompt jadi beda dari byte pertama -> Ollama/llama.cpp TIDAK BISA reuse KV-cache,
        seluruh prompt diproses ulang dari nol tiap giliran.

        Sekarang reminder ini dibuat sebagai pesan TERPISAH yang cuma disisipkan ke ekor
        list untuk kebutuhan invoke() saat ini saja (lihat _orchestrator) -- TIDAK pernah
        ikut disimpan ke state/checkpointer. messages[0] (system prompt asli) jadi selalu
        identik apa adanya di setiap giliran, sehingga prefix-nya stabil dan bisa di-cache.
        """
        return HumanMessage(
            content=(
                f"[🚨 PERINGATAN SISTEM: Kamu memiliki instruksi dari user yang masih tertunda:\n"
                f"{pending_tasks}\n"
                f"Segera tindak lanjuti jika user sudah memberikan data yang dibutuhkan!]"
            )
        )

    def _build_retry_reminder(self, percobaan_ke: int) -> SystemMessage:

        return HumanMessage(
            content=(
                f"[⚠️ PERINGATAN SISTEM: Respons kamu di giliran sebelumnya KOSONG "
                f"(percobaan ke-{percobaan_ke}). Lihat kembali hasil tool paling akhir "
                f"di atas dan analisis ulang rencanamu.\n"
                f"- Kalau rencanamu MEMANG perlu memanggil tool (misalnya untuk "
                f"menyimpan/menulis hasil akhir), PANGGIL tool itu SEKARANG -- "
                f"jangan cuma menuliskan niatmu dalam teks tanpa benar-benar "
                f"memanggilnya.\n"
                f"- Kalau kamu TIDAK butuh tool lagi, WAJIB tuliskan jawaban teks "
                f"akhir yang lengkap untuk user SEKARANG.\n"
                f"- Yang tidak boleh: mengirim respons kosong lagi, atau mengulang "
                f"tool yang PERSIS SAMA tanpa alasan baru.]"
            )
        )

    def _build_tool_repeat_reminder(self, nama_tools: str, jumlah: int) -> SystemMessage:
        """
        Ditempel di ekor list HANYA untuk invoke() saat ini (tidak ikut
        disimpan ke state/checkpointer) kalau giliran SEBELUMNYA terdeteksi
        memanggil tool (nama+args) yang PERSIS SAMA berturut-turut. Tujuannya
        kasih kesempatan model "sadar" dan berhenti sendiri sebelum
        DecisionRouter memaksa hard-stop di MAX_TOOL_REPEAT (agent_router.py).
        """
        return HumanMessage(
            content=(
                f"[🔁 PERINGATAN SISTEM: Kamu barusan memanggil tool [{nama_tools}] dengan "
                f"argumen yang PERSIS SAMA {jumlah}x berturut-turut. Hasilnya sudah ada di "
                f"riwayat obrolan di atas -- JANGAN panggil tool itu lagi dengan argumen "
                f"yang sama. Gunakan hasil yang sudah ada, ubah argumennya kalau memang "
                f"butuh data yang berbeda, atau langsung jelaskan ke user kalau kamu "
                f"sudah mentok/butuh info tambahan darinya.]"
            )
        )

    def _extract_pending_tasks(self, response_content: str) -> str:
        """Mengekstrak blok To-Do list (Scratchpad) dari balasan AI."""
        if not response_content:
            return ""
            
        marker = "### 📝 Status Tugas Aktif"
        if marker in response_content:
            parts = response_content.split(marker)
            if len(parts) > 1:
                return parts[1].strip()
        return ""

    @staticmethod
    def _ns_ke_detik(value):
        """Konversi nanodetik (format asli Ollama) ke detik, 3 desimal, buat logging biar gampang dibaca."""
        return round(value / 1e9, 3) if isinstance(value, (int, float)) else value

    def _orchestrator(self, state: AgentState):
        """
        Entry point yang dieksekusi oleh LangGraph.
        Strukturnya dipertahankan sesuai fungsi panggil_otak_llm aslinya.
        """
        # Gunakan list() agar tidak mengubah pointer asli
        #messages = list(state.get("messages", []))
        messages_raw = list(state.get("messages", []))

        messages = []
        for msg in messages_raw:
            # Jika ini adalah pesan AI, tapi teksnya kosong DAN tidak bawa tool calls, abaikan!
            if msg.type == "ai" and not msg.content.strip() and not getattr(msg, "tool_calls", None):
                print(f"\n[AIBrainProcessor.orchestrator]messages: {msg}\n")
                continue
            messages.append(msg)

        pending_tasks = state.get("pending_tasks", "")
        current_summary = state.get("summary", "") # <-- Ambil ringkasan saat ini

        revision_count = state.get("revision_count", 0) # <-- FIX RETRY KOSONG: hitungan percobaan ulang

        tool_repeat_count = state.get("tool_repeat_count", 0)
        last_tool_signature = state.get("last_tool_signature", "")
        last_tool_names = state.get("last_tool_names", "")

        # 1. [OPTIMASI KV-CACHE] System prompt SELALU statis apa adanya (base_prompt murni),
        # tidak pernah lagi disisipi teks dinamis di sini -- lihat penjelasan di _build_pending_reminder.
        if messages and isinstance(messages[0], SystemMessage):
            messages[0] = SystemMessage(content=self.base_prompt)
        else:
            messages.insert(0, SystemMessage(content=self.base_prompt))

        # 2. [TOGGLE MEKANISME OPTIMASI]
        if self.enable_optimization:
            messages_dioptimalkan, ringkasan_baru = optimasi_konteks_langchain(
                messages, current_summary, self.fast_llm,
                batas_pesan_inturn=self.batas_pesan_inturn,
                batas_karakter_inturn=self.batas_karakter_inturn,
                panjang_min_kompresi=self.panjang_min_kompresi,
            )
        else:
            # Mode Brutal: Bypass 100%, biarkan memori membengkak apa adanya
            print("\n[⚠️ WARNING] Optimasi Konteks DIMATIKAN. Memori dikirim utuh ke LLM!")
            messages_dioptimalkan = messages
            ringkasan_baru = current_summary

        if pending_tasks:
            messages_dioptimalkan = messages_dioptimalkan + [self._build_pending_reminder(pending_tasks)]

        if revision_count > 0:
            messages_dioptimalkan = messages_dioptimalkan + [self._build_retry_reminder(revision_count)]

        if tool_repeat_count > 0 and last_tool_names:
            messages_dioptimalkan = messages_dioptimalkan + [
                self._build_tool_repeat_reminder(last_tool_names, tool_repeat_count)
            ]

        current_task_desc = state.get("current_task_desc", "")
        current_task_desc_full = state.get("current_task_desc_full", "")
        current_skill_trace = state.get("current_skill_trace", [])
        mode_eksplorasi_tersimpan = state.get("mode_eksplorasi", None)

        task_desc_baru = None

        # current_task_desc yang tetap dipotong buat skill library.
        human_msg_lengkap_untuk_rag = None
        if not current_skill_trace:  # <-- UBAH KONDISI DI SINI:

            if messages_raw and messages_raw[-1].type == "human":
                pesan_human_terbaru = messages_raw[-1]
                if pesan_human_terbaru.content.strip():
                    task_desc_baru = pesan_human_terbaru.content.strip()[:300]
                    current_task_desc = task_desc_baru
                    human_msg_lengkap_untuk_rag = pesan_human_terbaru.content.strip()
                    current_task_desc_full = human_msg_lengkap_untuk_rag

        mode_eksplorasi_aktif = False
        mode_eksplorasi_baru_diputuskan = False
        if self.skill_library and current_task_desc:
            print(f"\n [Orchestrator] Agent mencari relevan skill dari pembelajaran...")

            skills_sukses = self.skill_library.cari_skill_relevan(
                current_task_desc, top_k=self.top_k_skill, status_filter="berhasil",
                min_similarity=self.min_similarity_skill_sukses,
            )
            
            skills_gagal = self.skill_library.cari_skill_relevan(
                current_task_desc, top_k=1, status_filter="gagal",
                maks_umur_detik=self.maks_umur_skill_gagal_detik,
                min_similarity=self.min_similarity_skill_gagal,
            )

            if skills_sukses:
                print(f"\n [Orchestrator] didapatkan skill sukses: {skills_sukses}")

            if skills_gagal:
                print(f"\n [Orchestrator] didapatkan skill gagal: {skills_gagal}")

            AMBANG_SIMILARITY_TINGGI = 0.85   # skor>=90 HARUS dibarengi similarity setinggi ini baru 0% eksplorasi
            AMBANG_SIMILARITY_RENDAH = 0.75   # di bawah ini, similarity terlalu lemah -> WAJIB eksplorasi apapun skor-nya

            def _probabilitas_untuk_skill(s: dict) -> float:
                skor = s.get("skor", 0)
                sim = s.get("similarity", 0)
                if skor < 85 or sim < AMBANG_SIMILARITY_RENDAH:
                    return 1.0
                if skor >= 90 and sim >= AMBANG_SIMILARITY_TINGGI:
                    return 0.0
                return 0.5

            if mode_eksplorasi_tersimpan is not None:
                # Sudah pernah diputuskan sebelumnya di task ini -- pakai apa adanya,
                # JANGAN di-roll ulang (biar konsisten sepanjang task).
                mode_eksplorasi_aktif = mode_eksplorasi_tersimpan
            elif skills_sukses:
                probabilitas_per_skill = [
                    (s["deskripsi"][:50], s.get("skor", 0), s.get("similarity", 0), _probabilitas_untuk_skill(s))
                    for s in skills_sukses
                ]
                probabilitas_eksplorasi = min(p for *_, p in probabilitas_per_skill)

                mode_eksplorasi_aktif = random.random() < probabilitas_eksplorasi
                mode_eksplorasi_baru_diputuskan = True

                print(
                    f"\n[🎲 Mode Eksplorasi] Evaluasi per-skill (deskripsi|skor|similarity|probabilitas): "
                    f"{probabilitas_per_skill} -> probabilitas akhir (ambil paling percaya diri) "
                    f"{probabilitas_eksplorasi*100:.0f}% -> "
                    f"{'EKSPLORASI (skill sukses disembunyikan)' if mode_eksplorasi_aktif else 'eksploitasi normal (skill sukses ditampilkan)'}"
                )

            if mode_eksplorasi_aktif:
                skills_sukses = []

            # 3. Format keduanya ke dalam satu prompt sistem
            teks_skill = self.skill_library.format_untuk_prompt(skills_sukses, skills_gagal)
            
            if teks_skill:
                messages_dioptimalkan = messages_dioptimalkan + [HumanMessage(content=f"[INFO SISTEM]\n{teks_skill}")]

                messages_dioptimalkan = messages_dioptimalkan + [
                    HumanMessage(content=(
                        "[PENGINGAT PRIORITAS]\n"
                        "Blok skill library di atas HANYALAH latar belakang historis, "
                        "BUKAN instruksi untuk sekarang. Yang WAJIB kamu ikuti adalah "
                        "instruksi eksplisit dari pesan user SEBELUMNYA di percakapan "
                        "ini -- kalau urutan langkah atau tool yang diminta user berbeda "
                        "dari referensi skill library, ABAIKAN referensi itu sepenuhnya "
                        "dan ikuti instruksi user apa adanya."
                    ))
                ]
        
        # ==========================================
        # 🛡️ Safety Net to handle: 
        # Jinja Exception: No user query found in messages.","type":"invalid_request_error"
        # ==========================================
        ada_human_msg = any(msg.type == "human" for msg in messages_dioptimalkan)
        if not ada_human_msg:
            # Jika semua instruksi user sudah usang dan terhapus oleh State Cleaner,
            # Ollama akan crash. Kita suntikkan instruksi dummy agar template Jinja aman.
            messages_dioptimalkan.append(
                HumanMessage(content="[Sistem Instuksi Otomatis] Lanjutkan analisismu berdasarkan data dari alat di atas.")
            )
        # ==========================================

        # --- 3d. GORILLA-STYLE DYNAMIC TOOL RETRIEVAL ---
        # Dipanggil TEPAT SEBELUM invoke() -- bukan di step lain -- supaya query
        # RAG-nya sedekat mungkin dengan kondisi TERKINI.
        if self.tool_registry is not None and self.gorilla_aktif:
            basis_query = current_task_desc_full or current_task_desc
            konteks_terkini = ""
            if current_skill_trace:  # <-- baru diperkaya kalau memang sudah mid-task
                for m in reversed(messages_raw):
                    if m.type in ("ai", "tool") and (m.content or "").strip():
                        konteks_terkini = m.content.strip()[:300]
                        break

            query_rag = " ".join(filter(None, [basis_query, konteks_terkini])).strip()
            if not query_rag and messages_raw and messages_raw[-1].type == "human":
                query_rag = messages_raw[-1].content.strip()

            tools_relevan = self.tool_registry.get_relevant_tools(
                query_rag, top_k=self.top_k_tools
            ) if query_rag else self._tools_fallback

            print(
                f"\n[🦍 Tool-RAG Gorilla] Query: \"{query_rag[:120]}\" -> "
                f"{len(tools_relevan)} tool dipilih dari {len(self._tools_fallback)}: "
                f"{[t.name for t in tools_relevan]}"
            )
            llm_untuk_invoke = self._llm_mentah.bind_tools(tools_relevan)
        else:
            llm_untuk_invoke = self._llm_mentah.bind_tools(self._tools_fallback)

        print("\n[Log Sistem] AI Utama sedang menganalisis input atau menyusun jawaban...")
        
        # 4. Panggil LLM (DIBUNGKUS TRY-EXCEPT)
        try:
            response = llm_untuk_invoke.invoke(messages_dioptimalkan)
        except Exception as e:
            error_str = str(e)
            # Tangkap error JSON terpotong dari Ollama
            if "unexpected end of JSON input" in error_str or "invalid tool call" in error_str.lower():
                print(f"\n[⚠️ OLLAMA CRASH] LLM gagal memformat JSON (terlalu panjang/terpotong). Membangkitkan respons darurat...")
                from langchain_core.messages import AIMessage
                
                # Ciptakan respons darurat yang memuat invalid_tool_calls
                response = AIMessage(
                    content="",
                    invalid_tool_calls=[{
                        "name": "tulis_file",
                        "args": "ERROR_JSON_TERPOTONG",
                        "id": "error_id_darurat",
                        "error": "unexpected end of JSON input - Output kodemu terlalu panjang dan terpotong. Coba pecah menjadi bagian yang lebih kecil atau tulis bagian utamanya saja."
                    }]
                )
            else:
                # Jika error lain (misal koneksi terputus), lemparkan ke atas
                raise e

        # 5. [OPTIMASI KV-CACHE] Log metrik asli dari Ollama, buat verifikasi cache kepakai atau tidak.
        # Bandingkan 'prompt_eval_time' antar giliran DI THREAD YANG SAMA: kalau caching jalan,
        # giliran ke-2 dst seharusnya jauh lebih kecil dari giliran pertama (bukan diproses dari nol lagi).
        meta = getattr(response, "response_metadata", {}) or {}
        print(
            "\n[⏱️ METRIK OLLAMA] "
            f"prompt_tokens={meta.get('prompt_eval_count')} "
            f"prompt_eval_time={self._ns_ke_detik(meta.get('prompt_eval_duration'))}s | "
            f"gen_tokens={meta.get('eval_count')} "
            f"gen_time={self._ns_ke_detik(meta.get('eval_duration'))}s | "
            f"total_time={self._ns_ke_detik(meta.get('total_duration'))}s"
        )
        
        print("\n--- [DAPUR AGENT: APA YANG DIPIKIRKAN LLM?] ---")
        print(f"Content: {response.content}") 
        print(f"Tool Calls: {response.tool_calls}")
        print(f"Invalid Tool Calls: {response.invalid_tool_calls }")
        print("----------------------------------------------\n")
        
        # 6. Siapkan state balasan (Simpan hasil ringkasan agar permanen di DB)
        update_state = {
            "messages": [response],
            "summary": ringkasan_baru 
        }

        # --- Rekam tool_calls giliran ini ke jejak skill task aktif ---
        if response.tool_calls:
            trace_entry = [
                {"name": tc.get("name"), "args": tc.get("args")} for tc in response.tool_calls
            ]
            update_state["current_skill_trace"] = trace_entry  # numpuk via operator.add

        if task_desc_baru:
            update_state["current_task_desc"] = task_desc_baru
            update_state["current_task_desc_full"] = human_msg_lengkap_untuk_rag or task_desc_baru

        # Simpan keputusan mode eksplorasi HANYA kalau baru diputuskan turn
        # ini (lihat blok "MODE EKSPLORASI" di atas) -- supaya tetap konsisten
        # sepanjang task yang sama, tidak di-roll ulang tiap giliran.
        if mode_eksplorasi_baru_diputuskan:
            update_state["mode_eksplorasi"] = mode_eksplorasi_aktif

        response_kosong = not response.content.strip() and not getattr(response, "tool_calls", None)
        if response_kosong:
            update_state["revision_count"] = 1
        elif revision_count > 0:
            update_state["revision_count"] = -revision_count  # reset ke 0

        if response.tool_calls:
            new_signature = _signature_tool_calls(response.tool_calls)
            new_names = ", ".join(
                tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                for tc in response.tool_calls
            )
            if new_signature == last_tool_signature and last_tool_signature:
                update_state["tool_repeat_count"] = 1
                print(
                    f"\n[🔁 Tool Repeat Guard] Tool [{new_names}] dipanggil ULANG dengan "
                    f"argumen sama (ke-{tool_repeat_count + 1}x berturut-turut)."
                )
            elif tool_repeat_count > 0:
                update_state["tool_repeat_count"] = -tool_repeat_count  # reset, tool/argumen beda
            update_state["last_tool_signature"] = new_signature
            update_state["last_tool_names"] = new_names
        else:
            # Tidak ada tool call di giliran ini -> reset signature & counter
            if tool_repeat_count > 0:
                update_state["tool_repeat_count"] = -tool_repeat_count
            update_state["last_tool_signature"] = ""
            update_state["last_tool_names"] = ""

        # 6c.Tangkap sinyal tools_reward / tools_gagal / tools_batal
        for tc in (response.tool_calls or []):
            nama_tool = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            
            # --- TAMBAHAN UNTUK RESET/BATAL ---
            if nama_tool == "tools_batal":
                print("[Skill Library] 🧹 Membatalkan dan mereset jejak task yang menggantung.")
                update_state["current_skill_trace"] = None
                update_state["current_task_desc"] = ""
                update_state["current_task_desc_full"] = ""
                update_state["mode_eksplorasi"] = None
                continue
                
            if nama_tool in ("tools_reward", "tools_gagal") and self.skill_library:
                
                # Cegah double-save jika trace sudah kosong
                if not current_skill_trace:
                    print(f"[Skill Library] Abaikan {nama_tool} karena trace kosong (Double call).")
                    update_state["current_skill_trace"] = None
                    update_state["current_task_desc"] = ""
                    update_state["current_task_desc_full"] = ""
                    update_state["mode_eksplorasi"] = None
                    continue

                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                status = "berhasil" if nama_tool == "tools_reward" else "gagal"
                
                # Ekstrak skor
                skor_nilai = args.get("skor", 0)
                try:
                    skor_nilai = int(skor_nilai)
                except (ValueError, TypeError):
                    skor_nilai = 0

                self.skill_library.simpan_skill(
                    deskripsi_task=current_task_desc or "(deskripsi task tidak diset)",
                    trace=current_skill_trace,
                    catatan_hasil=args.get("catatan_hasil", ""),
                    status=status,
                    skor=skor_nilai,
                )
                
                # reset trace & task desc utk task berikutnya
                update_state["current_skill_trace"] = None
                update_state["current_task_desc"] = ""
                update_state["current_task_desc_full"] = ""
                update_state["mode_eksplorasi"] = None
                
        # 7. simpan status task
        if response.content:
            update_state["pending_tasks"] = self._extract_pending_tasks(response.content)
        else:
            # Jika respon hanya memanggil tool tanpa teks, biarkan task pending sebelumnya (jangan ditimpa string kosong)
            # Kecuali jika ingin meresetnya. Untuk amannya, kita abaikan update jika tidak ada text.
            pass

        # ==========================================
        # 8. [TAMBAHAN BARU] HAPUS PESAN LAMA DARI SQLITE (STATE CLEANER)
        # ==========================================
        # Agar saat sesi lama di-load, SQLite tidak menarik ratusan pesan ke RAM.
        # Angka ini adalah sisa pesan yang dibiarkan "hidup" di database.
        BATAS_SIMPAN_DB = 5 # 10
        semua_pesan_asli = state.get("messages", [])

        if len(semua_pesan_asli) > BATAS_SIMPAN_DB:
            # Ambil semua pesan dari awal hingga batas pemotongan
            pesan_usang = semua_pesan_asli[:-BATAS_SIMPAN_DB]
            
            # Buat list perintah RemoveMessage berdasarkan ID pesan
            # (Pastikan pesan memiliki ID, LangGraph otomatis memberikannya)
            perintah_hapus = [RemoveMessage(id=msg.id) for msg in pesan_usang if msg.id]
            
            # Gabungkan perintah hapus ke dalam array messages yang akan di-update
            # LangGraph akan membaca RemoveMessage ini dan menghapusnya dari SQLite!
            update_state["messages"] = perintah_hapus + update_state["messages"]
            
            print(f"\n[🧹 State Cleaner] Menginstruksikan SQLite untuk menghapus {len(perintah_hapus)} pesan usang dari memori hard disk!")

        return update_state

    def __call__(self, state: AgentState) -> dict:
        return self._orchestrator(state)