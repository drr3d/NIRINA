import time, json
import concurrent.futures
import hashlib
from typing import Any, Optional

from langchain_core.messages import SystemMessage, HumanMessage

from .agent_nodes import (
    # --- helper murni (context optimization, tool-repeat guard) ---
    _bukan_nudge_sistem,
    buat_ringkasan_memori,
    #_bangun_cleaned_messages,
    #_injeksi_ringkasan,
    #optimasi_konteks_langchain,
    # --- state & konstanta ---
    AgentState,
    NAMA_TOOL_META_BUKAN_BAGIAN_TRACE,
    # --- sub-komponen spesialisasi ---
    SkillLibraryOrchestrator,
    GorillaToolSelector,
    # --- state cleaner (SQLite pruning) ---
    hitung_perintah_hapus_pesan_lama,
    hapus_pesan_task_dibatalkan,
)

# ==========================================
# ------------ HELPER Function -------------
# ==========================================
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

def _signature_tool_calls(tool_calls: list) -> str:
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

def _injeksi_ringkasan(cleaned_messages: list, ringkasan_baru: str) -> list:
    if not ringkasan_baru:
        return cleaned_messages
    pesan_ingatan = SystemMessage(
        content=f"--- INGATAN JANGKA PANJANG AI ---\n{ringkasan_baru}\n---------------------------------"
    )
    # [KV-CACHE TRICK]: Selalu sisipkan di index 1!
    # Index 0 harus selalu base_prompt murni agar KV-Cache Ollama tidak hancur.
    if len(cleaned_messages) > 0 and cleaned_messages[0].type == "system":
        cleaned_messages.insert(1, pesan_ingatan)
    else:
        cleaned_messages.insert(0, pesan_ingatan)
    return cleaned_messages

def _panjang_args_tool_calls(tool_calls) -> int:
    """Total panjang (karakter) semua argumen tool_calls, dalam bentuk JSON. Dipakai
    untuk cek ambang kompresi & buat katup ukuran dalam-giliran (lihat di bawah)."""
    total = 0
    for tc in (tool_calls or []):
        args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
        total += len(json.dumps(args, default=str))
    return total

def _bangun_cleaned_messages(
    messages,
    batas_pesan_inturn: int = 15,
    batas_karakter_inturn: int = 20_000,
    panjang_min_kompresi: int = 300,
    ringkasan_aktif: bool = True,
):
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

        # Cek ambang pakai akumulasi SEBELUM pesan ini ditambahkan.
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
                if ringkasan_aktif:
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
                if ringkasan_aktif:
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
                    if ringkasan_aktif:
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
                    if ringkasan_aktif:
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

    return cleaned_messages, pesan_untuk_diringkas

# ==========================================
# --- 3. DEFINISI NODE (KOMPONEN AI) ---
# ==========================================
class AIBrainProcessor:
    """
    Komponen Otak Utama (Brain Node) untuk AI Agent.

    Bertindak sebagai KOORDINATOR: susun konteks -> panggil LLM -> susun
    update state (lihat urutan langkahnya di docstring _orchestrator). Detail
    dua sub-sistem yang cukup besar didelegasikan ke kelas terpisah supaya
    masing-masing bisa diubah/ditest sendiri tanpa menyentuh alur utama:
      - SkillLibraryOrchestrator (self._skills) -> retrieval skill, mode
        eksplorasi, simpan skill baru.
      - GorillaToolSelector (self._tools) -> Tool-RAG dinamis (pilih subset
        tool yang paling relevan tiap giliran).
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

        skill_library: Any = None,   # <-- instance SkillLibrary, opsional
        top_k_skill: int = 3,
        maks_umur_skill_gagal_detik: Optional[float] = 2 * 24 * 3600,
        min_similarity_skill_sukses: float = 0.80,
        min_similarity_skill_gagal: float = 0.65,

        # --- GORILLA-STYLE DYNAMIC TOOL RETRIEVAL ---
        tool_registry: Any = None,   # <-- instance/class ToolRegistry, opsional
        top_k_tools: int = 8,
        maks_tool_dipaksa_manual: int = 6,  # <-- lihat GorillaToolSelector.proses_permintaan_tool_manual
        llm_rag: Any = None,  # <-- [BARU] LLM ringan khusus HyDE/rerank di GorillaToolSelector.retrieve(); default None = fallback ke llm_model (perilaku lama, 100% backward compatible). Lihat docstring GorillaToolSelector untuk alasannya.

        batas_simpan_db: int = 10,
        max_humanmsgs_taskdesccutoff: int = 1000,

        # --- [HARDENING KONKURENSI] Thread-pool I/O paralel (lihat _jalankan_io_paralel) ---
        max_io_workers: int = 4,
        io_timeout_detik: Optional[float] = 20.0,
    ):
        self.base_prompt = base_prompt
        self.fast_llm = fast_llm
        self.enable_optimization = enable_optimization
        self.batas_pesan_inturn = batas_pesan_inturn
        self.batas_karakter_inturn = batas_karakter_inturn
        self.panjang_min_kompresi = panjang_min_kompresi
        self.batas_simpan_db = batas_simpan_db
        self.max_humanmsgs_taskdesccutoff = max_humanmsgs_taskdesccutoff
        self.io_timeout_detik = io_timeout_detik

        self._io_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_io_workers, thread_name_prefix="agent_io"
        )

        self._skills = SkillLibraryOrchestrator(
            skill_library=skill_library,
            top_k_skill=top_k_skill,
            maks_umur_skill_gagal_detik=maks_umur_skill_gagal_detik,
            min_similarity_skill_sukses=min_similarity_skill_sukses,
            min_similarity_skill_gagal=min_similarity_skill_gagal,
        )
        self._tools = GorillaToolSelector(
            tool_registry=tool_registry,
            tools_fallback=tools_list,
            llm_mentah=llm_model,
            top_k_tools=top_k_tools,
            maks_tool_dipaksa_manual=maks_tool_dipaksa_manual,
            llm_rag=llm_rag,
        )

    # --- Properti backward-compat
    @property
    def skill_library(self):
        return self._skills.skill_library

    @property
    def tool_registry(self):
        return self._tools.tool_registry

    @property
    def gorilla_aktif(self):
        return self._tools.aktif_default

    def _build_pending_reminder(self, pending_tasks: str) -> HumanMessage:
        """
        Dulu teks ini disambung ke system prompt (messages[0]),
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

    def _build_retry_reminder(self, percobaan_ke: int) -> HumanMessage:

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

    def _build_tool_repeat_reminder(self, nama_tools: str, jumlah: int) -> HumanMessage:
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

    # ==========================================
    # --- Langkah 1: bersihkan input & system prompt ---
    # ==========================================
    @staticmethod
    def _bersihkan_pesan_ai_kosong(messages_raw: list) -> list:
        """Buang AIMessage yang teksnya kosong DAN tidak bawa tool_calls
        (sampah dari Ollama yang gagal generate apa-apa) SEBELUM masuk
        context-optimizer. TIDAK mengubah list `messages_raw` asli -- caller
        (deteksi task-anchor & Tool-RAG) tetap butuh riwayat ASLI apa adanya."""
        hasil = []
        for msg in messages_raw:
            if msg.type == "ai" and not msg.content.strip() and not getattr(msg, "tool_calls", None):
                print(f"\n[AIBrainProcessor.orchestrator]messages: {msg}\n")
                continue
            hasil.append(msg)
        return hasil

    def _pasang_system_prompt(self, messages: list) -> list:
        """[OPTIMASI KV-CACHE] System prompt SELALU statis apa adanya
        (base_prompt murni), tidak pernah disisipi teks dinamis di sini --
        lihat penjelasan lengkap di _build_pending_reminder."""
        if messages and isinstance(messages[0], SystemMessage):
            messages[0] = SystemMessage(content=self.base_prompt)
        else:
            messages.insert(0, SystemMessage(content=self.base_prompt))
        return messages

    # ==========================================
    # --- Optimasi konteks (versi non-paralel) ---
    # ==========================================
    """
    def _optimasi_konteks(self, messages: list, current_summary: str):
        if not self.enable_optimization:
            # Mode Brutal: Bypass 100%, biarkan memori membengkak apa adanya
            print("\n[⚠️ WARNING] Optimasi Konteks DIMATIKAN. Memori dikirim utuh ke LLM!")
            return messages, current_summary
        return optimasi_konteks_langchain(
            messages, current_summary, self.fast_llm,
            batas_pesan_inturn=self.batas_pesan_inturn,
            batas_karakter_inturn=self.batas_karakter_inturn,
            panjang_min_kompresi=self.panjang_min_kompresi,
        )
    """
    def _jalankan_io_paralel(
        self, *, messages: list, current_summary: str, current_task_desc: str,
        query_rag: str, gorilla_aktif_override: Optional[bool],
    ) -> dict:
        waktu_mulai = time.monotonic()

        if self.enable_optimization:
            cleaned_messages, pesan_untuk_diringkas = _bangun_cleaned_messages(
                messages,
                batas_pesan_inturn=self.batas_pesan_inturn,
                batas_karakter_inturn=self.batas_karakter_inturn,
                panjang_min_kompresi=self.panjang_min_kompresi,
                ringkasan_aktif=bool(self.fast_llm),
            )
        else:
            print("\n[⚠️ WARNING] Optimasi Konteks DIMATIKAN. Memori dikirim utuh ke LLM!")
            cleaned_messages, pesan_untuk_diringkas = messages, []

        future_ringkasan = None
        if pesan_untuk_diringkas and self.fast_llm:
            future_ringkasan = self._io_pool.submit(
                buat_ringkasan_memori, pesan_untuk_diringkas, self.fast_llm, current_summary
            )
        future_sukses = self._io_pool.submit(self._skills.cari_sukses, current_task_desc)
        future_gagal = self._io_pool.submit(self._skills.cari_gagal, current_task_desc)
        future_tools = self._io_pool.submit(self._tools.retrieve, query_rag, gorilla_aktif_override)

        ringkasan_baru = current_summary
        if future_ringkasan is not None:
            try:
                print("\n[🧠 Memory Manager] Mengompresi masa lalu menggunakan Fast LLM (paralel)...")
                ringkasan_baru = future_ringkasan.result(timeout=self.io_timeout_detik)
            except concurrent.futures.TimeoutError:
                print(f"\n[⚠️ Memory Manager] Timeout ({self.io_timeout_detik}s) meringkas -- pakai ringkasan lama apa adanya.")
            except Exception as e:
                print(f"\n[⚠️ Memory Manager] Gagal meringkas ({e}) -- pakai ringkasan lama apa adanya.")

        try:
            skills_sukses = future_sukses.result(timeout=self.io_timeout_detik)
        except concurrent.futures.TimeoutError:
            print(f"\n[⚠️ Skill Library] Timeout ({self.io_timeout_detik}s) cari skill sukses -- lanjut tanpa itu giliran ini.")
            skills_sukses = []
        except Exception as e:
            print(f"\n[⚠️ Skill Library] Gagal cari skill sukses ({e}) -- lanjut tanpa itu giliran ini.")
            skills_sukses = []

        try:
            skills_gagal = future_gagal.result(timeout=self.io_timeout_detik)
        except concurrent.futures.TimeoutError:
            print(f"\n[⚠️ Skill Library] Timeout ({self.io_timeout_detik}s) cari skill gagal -- lanjut tanpa itu giliran ini.")
            skills_gagal = []
        except Exception as e:
            print(f"\n[⚠️ Skill Library] Gagal cari skill gagal ({e}) -- lanjut tanpa itu giliran ini.")
            skills_gagal = []

        try:
            tools_relevan = future_tools.result(timeout=self.io_timeout_detik)
        except concurrent.futures.TimeoutError:
            print(f"\n[⚠️ Tool-RAG Gorilla] Timeout ({self.io_timeout_detik}s) retrieval -- fallback ke SEMUA tool.")
            tools_relevan = list(self._tools.tools_fallback)
        except Exception as e:
            print(f"\n[⚠️ Tool-RAG Gorilla] Retrieval gagal ({e}) -- fallback ke SEMUA tool.")
            tools_relevan = list(self._tools.tools_fallback)

        cleaned_messages = _injeksi_ringkasan(cleaned_messages, ringkasan_baru)

        print(f"\n[⏱️ I/O Paralel] Selesai dalam {time.monotonic() - waktu_mulai:.3f}s (ringkasan+skill+tool-rag bersamaan).")

        return {
            "cleaned_messages": cleaned_messages,
            "ringkasan_baru": ringkasan_baru,
            "skills_sukses": skills_sukses,
            "skills_gagal": skills_gagal,
            "tools_relevan": tools_relevan,
        }

    def _tambahkan_reminder(self, messages_dioptimalkan, pending_tasks, revision_count, tool_repeat_count, last_tool_names):
        """Tempel reminder SEMENTARA (cuma buat invoke() saat ini, TIDAK ikut
        disimpan ke state) di ekor list -- lihat masing-masing _build_*_reminder."""
        if pending_tasks:
            messages_dioptimalkan = messages_dioptimalkan + [self._build_pending_reminder(pending_tasks)]
        if revision_count > 0:
            messages_dioptimalkan = messages_dioptimalkan + [self._build_retry_reminder(revision_count)]
        if tool_repeat_count > 0 and last_tool_names:
            messages_dioptimalkan = messages_dioptimalkan + [
                self._build_tool_repeat_reminder(last_tool_names, tool_repeat_count)
            ]
        return messages_dioptimalkan

    # ==========================================
    # --- Langkah 3: deteksi anchor task baru ---
    # ==========================================
    def _deteksi_task_baru(
        self, messages_raw, current_skill_trace, current_task_desc,
        current_task_desc_full, id_pesan_task_aktif,
    ):
        """Kalau belum ada task aktif (current_skill_trace kosong) DAN pesan
        TERAKHIR adalah instruksi asli user (bukan nudge sistem), anggap itu
        anchor task baru -- dipakai skill library & Tool-RAG Gorilla."""
        task_desc_baru = None
        human_msg_lengkap_untuk_rag = None

        if (
            not current_skill_trace
            and messages_raw and messages_raw[-1].type == "human"
            and _bukan_nudge_sistem(messages_raw[-1].content)
        ):
            pesan_human_terbaru = messages_raw[-1]
            if pesan_human_terbaru.content.strip():
                task_desc_baru = pesan_human_terbaru.content.strip()[:self.max_humanmsgs_taskdesccutoff]
                current_task_desc = task_desc_baru
                human_msg_lengkap_untuk_rag = pesan_human_terbaru.content.strip()
                current_task_desc_full = human_msg_lengkap_untuk_rag
                id_pesan_task_aktif = getattr(pesan_human_terbaru, "id", None)

        return task_desc_baru, current_task_desc, current_task_desc_full, id_pesan_task_aktif, human_msg_lengkap_untuk_rag

    # ==========================================
    # --- Langkah 5: safety-net Jinja "No user query found" ---
    # ==========================================
    @staticmethod
    def _pastikan_ada_human_message(messages_dioptimalkan, current_task_desc_full, current_task_desc):
        """
        🛡️ Safety net: Ollama/Jinja crash kalau TIDAK ADA HumanMessage sama
        sekali di prompt ("No user query found in messages"). Bisa kejadian
        kalau semua instruksi user sudah dikompres/dihapus State Cleaner.
        Suntikkan instruksi pengingat/dummy supaya template tetap valid dan
        task tidak hilang begitu saja.
        """
        ada_human_msg = any(msg.type == "human" for msg in messages_dioptimalkan)
        if ada_human_msg:
            return messages_dioptimalkan

        tugas_pengingat = current_task_desc_full or current_task_desc
        if tugas_pengingat:
            isi_pengingat = (
                "[Sistem Instruksi Otomatis] Instruksi ASLI kamu (sudah terhapus dari "
                "riwayat pesan karena manajemen memori, TAPI TETAP BERLAKU dan WAJIB "
                f"kamu selesaikan):\n\n\"{tugas_pengingat}\"\n\nLanjutkan menyelesaikan "
                "instruksi itu berdasarkan data dari alat-alat yang sudah kamu jalankan "
                "di atas -- JANGAN improvisasi topik baru yang tidak diminta."
            )
        else:
            # Jika semua instruksi user sudah usang dan terhapus oleh State Cleaner,
            # Ollama akan crash. Kita suntikkan instruksi dummy agar template Jinja aman.
            isi_pengingat = "[Sistem Instuksi Otomatis] Lanjutkan analisismu berdasarkan data dari alat di atas."

        return messages_dioptimalkan + [HumanMessage(content=isi_pengingat)]

    # ==========================================
    # --- Langkah 6: panggil LLM (dibungkus try-except) ---
    # ==========================================
    @staticmethod
    def _invoke_llm_aman(llm_untuk_invoke, messages_dioptimalkan):
        """Bungkus llm.invoke() -- kalau Ollama gagal memformat JSON tool_call
        (biasanya karena output kepotong/kepanjangan), jangan biarkan seluruh
        request GAGAL TOTAL: bangkitkan AIMessage darurat berisi
        invalid_tool_calls supaya alur tetap bisa lanjut & user/AI tahu apa
        yang salah, alih-alih exception naik sampai crash node LangGraph."""
        from langchain_core.messages import AIMessage
        try:
            return llm_untuk_invoke.invoke(messages_dioptimalkan)
        except Exception as e:
            error_str = str(e)
            if "unexpected end of JSON input" not in error_str and "invalid tool call" not in error_str.lower():
                # Error lain (misal koneksi terputus) -- lemparkan ke atas apa adanya.
                raise
            print(f"\n[⚠️ OLLAMA CRASH] LLM gagal memformat JSON (terlalu panjang/terpotong). Membangkitkan respons darurat...")
            return AIMessage(
                content="",
                invalid_tool_calls=[{
                    "name": "tulis_file",
                    "args": "ERROR_JSON_TERPOTONG",
                    "id": "error_id_darurat",
                    "error": "unexpected end of JSON input - Output kodemu terlalu panjang dan terpotong. Coba pecah menjadi bagian yang lebih kecil atau tulis bagian utamanya saja."
                }]
            )

    def _log_metrik(self, response):
        """Log metrik asli Ollama (buat verifikasi KV-cache kepakai atau
        tidak) + isi mentah respons LLM (content/tool_calls/invalid_tool_calls)
        -- murni observability, tidak mengubah apapun di state."""
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
        print(f"Invalid Tool Calls: {response.invalid_tool_calls}")
        print("----------------------------------------------\n")

    # ==========================================
    # --- Langkah 7: susun update_state balasan ---
    # ==========================================
    @staticmethod
    def _update_tool_repeat_signature(update_state, response, tool_repeat_count, last_tool_signature):
        """Deteksi apakah giliran ini mengulang tool_call (nama+args) yang
        PERSIS SAMA dgn giliran sebelumnya (lihat _signature_tool_calls) --
        dipakai guard MAX_TOOL_REPEAT di agent_router.py."""
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

    def _proses_sinyal_tool_khusus(self, update_state, response, tools_dipaksa_manual):
        """Tangkap 'sinyal' tool khusus di tool_calls giliran ini -- ini
        BUKAN eksekusi tool (itu tetap lewat ToolNode seperti biasa), cuma
        efek samping di STATE yang perlu dicatat begitu AI memutuskan
        memanggilnya:
          - atur_gorilla_tool_rag    -> toggle Tool-RAG per SESI
          - minta_tool_manual        -> paksa satu tool ikut ter-bind
            mulai giliran berikutnya, buat kasus tool itu GENUINELY ada
            tapi kelewat oleh semantic search Tool-RAG (lihat
            GorillaToolSelector.proses_permintaan_tool_manual)
          - tools_batal              -> buang jejak skill task yang menggantung
          - tools_reward/tools_gagal -> simpan skill baru (delegasi ke
            SkillLibraryOrchestrator) lalu reset jejak task
        """
        for tc in (response.tool_calls or []):
            nama_tool = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})

            # --- Catch SINYAL TOGGLE GORILLA TOOL-RAG (PER-SESI) ---
            if nama_tool == "atur_gorilla_tool_rag":
                aktif_baru = bool(args.get("aktif", True))
                update_state["gorilla_aktif_override"] = aktif_baru
                print(
                    f"\n[⚙️ Runtime Toggle -- PER SESI] Tool-RAG Gorilla "
                    f"{'DIAKTIFKAN' if aktif_baru else 'DINONAKTIFKAN'} untuk percakapan ini."
                )

            # --- Catch SINYAL INJEKSI TOOL MANUAL ---
            if nama_tool == "minta_tool_manual":
                update_state.update(
                    self._tools.proses_permintaan_tool_manual(
                        args.get("nama_tool", ""), tools_dipaksa_manual
                    )
                )

    def _bangun_update_state(
        self, *, response, ringkasan_baru, revision_count,
        tool_repeat_count, last_tool_signature,
        current_skill_trace, tools_dipaksa_manual,
        task_desc_baru, human_msg_lengkap_untuk_rag, id_pesan_task_aktif,
        mode_eksplorasi_aktif, mode_eksplorasi_baru_diputuskan,
        keputusan_rag_baru=None,
    ):
        """Susun dict update_state lengkap untuk giliran ini (Simpan hasil
        ringkasan agar permanen di DB, jejak skill, task desc, dst).

        `keputusan_rag_baru`: snapshot kandidat Tool-RAG giliran INI (hasil
        GorillaToolSelector.pilih_llm(), None kalau Tool-RAG nonaktif) --
        dinumpuk ke current_rag_candidates_trace via reducer operator.add.
        `current_rag_candidates_trace`: riwayat kandidat yang SUDAH numpuk
        dari giliran-giliran SEBELUMNYA di task ini (dibaca dari state oleh
        _orchestrator) -- diteruskan apa adanya ke _proses_sinyal_tool_khusus
        supaya saat tools_reward/tools_gagal terpicu, SELURUH riwayat task
        ini (bukan cuma giliran terakhir) ikut tersimpan ke skill library."""
        update_state = {
            "messages": [response],
            "summary": ringkasan_baru,
            "baru_saja_tutup_task": False,
        }

        tool_calls_relevan = [
            {"name": tc.get("name"), "args": tc.get("args")}
            for tc in (response.tool_calls or [])
            if tc.get("name") not in NAMA_TOOL_META_BUKAN_BAGIAN_TRACE
        ]

        if tool_calls_relevan:
            entries_baru = list(tool_calls_relevan)
            if not current_skill_trace or current_skill_trace[-1] == "END":
                entries_baru = ["START"] + entries_baru
            update_state["current_skill_trace"] = entries_baru  # numpuk via operator.add
        elif response.content.strip() and not response.tool_calls:
            # Jawaban final MURNI ke user (ada teks, TANPA tool_calls sama
            # sekali) -> tutup putaran ini, tapi cuma kalau memang lagi ada
            # trace berjalan & belum pernah ditutup END sebelumnya (hindari
            # dobel END kalau AI nanya klarifikasi 2x berturut-turut).
            if current_skill_trace and current_skill_trace[-1] != "END":
                update_state["current_skill_trace"] = ["END"]

        if keputusan_rag_baru:
            keputusan_rag_baru = {**keputusan_rag_baru, "ada_tool_call": bool(response.tool_calls)}
            update_state["current_rag_candidates_trace"] = [keputusan_rag_baru]

        if task_desc_baru:
            update_state["current_task_desc"] = task_desc_baru
            update_state["current_task_desc_full"] = human_msg_lengkap_untuk_rag or task_desc_baru
            update_state["id_pesan_task_aktif"] = id_pesan_task_aktif

        # Simpan keputusan mode eksplorasi HANYA kalau baru diputuskan turn
        # ini -- supaya tetap konsisten sepanjang task yang sama, tidak
        # di-roll ulang tiap giliran.
        if mode_eksplorasi_baru_diputuskan:
            update_state["mode_eksplorasi"] = mode_eksplorasi_aktif

        response_kosong = not response.content.strip() and not getattr(response, "tool_calls", None)
        if response_kosong:
            update_state["revision_count"] = 1
        elif revision_count > 0:
            update_state["revision_count"] = -revision_count  # reset ke 0

        self._update_tool_repeat_signature(update_state, response, tool_repeat_count, last_tool_signature)
        self._proses_sinyal_tool_khusus(update_state, response, tools_dipaksa_manual)

        # simpan status task
        if response.content:
            update_state["pending_tasks"] = self._extract_pending_tasks(response.content)
        # Jika respon hanya memanggil tool tanpa teks, biarkan task pending
        # sebelumnya (jangan ditimpa string kosong).

        return update_state

    # ==========================================
    # --- Langkah 8: state cleaner (SQLite) ---
    # ==========================================
    def _bersihkan_pesan_lama(self, state: "AgentState", update_state: dict, id_pesan_task_aktif) -> dict:
        """Agar saat sesi lama di-load, SQLite tidak menarik ratusan pesan ke
        RAM. `self.batas_simpan_db` adalah sisa pesan yang dibiarkan "hidup"
        di database. Lihat `hitung_perintah_hapus_pesan_lama` untuk aturan
        pesan mana yang boleh/tidak boleh dihapus."""
        semua_pesan_asli = state.get("messages", [])
        anchor_id = update_state.get("id_pesan_task_aktif", id_pesan_task_aktif)

        perintah_hapus = hitung_perintah_hapus_pesan_lama(
            semua_pesan_asli, anchor_id=anchor_id, batas_simpan_db=self.batas_simpan_db
        )
        if perintah_hapus:

            update_state["messages"] = perintah_hapus + update_state["messages"]
            print(f"\n[🧹 State Cleaner] Menginstruksikan SQLite untuk menghapus {len(perintah_hapus)} pesan usang dari memori hard disk!")

        return update_state

    def _cek_hasil_hitl_reward(self, messages_raw, current_task_desc, current_skill_trace,
                            current_rag_candidates_trace, id_toolmsg_reward_terproses,
                            id_pesan_task_aktif_lama=None):
        from langchain_core.messages import ToolMessage
        if not messages_raw or not isinstance(messages_raw[-1], ToolMessage):
            return {}
        msg = messages_raw[-1]
        if msg.name not in ("tools_reward", "tools_gagal", "tools_batal"):
            return {}
        if msg.tool_call_id == id_toolmsg_reward_terproses:
            return {}  # sudah pernah diproses, skip

        isi = str(msg.content or "")
        update = {"id_toolmsg_reward_terproses": msg.tool_call_id}

        if isi.startswith("SYSTEM ABORT"):
            print(f"[Skill Library] 🚫 {msg.name} DIBATALKAN user via HITL -- skip simpan_skill, task TETAP lanjut.")
            return update  # nggak reset apa-apa, task lanjut seperti biasa

        if msg.name == "tools_batal":
            print("[Skill Library] 🧹 tools_batal disetujui -- reset jejak task.")
            update.update(SkillLibraryOrchestrator.reset_task_state())

            pesan_dihapus = hapus_pesan_task_dibatalkan(messages_raw, id_pesan_task_aktif_lama)
            if pesan_dihapus:
                update["messages"] = pesan_dihapus
                print(f"[🧹 Purge Task Dibatalkan] Menghapus {len(pesan_dihapus)} pesan task lama seketika.")
            return update

        # tools_reward / tools_gagal disetujui -> cari args ASLI dari AIMessage
        # yang punya tool_calls dgn id yang sama, baru simpan_skill() beneran
        args_asli = {}
        for m in reversed(messages_raw):
            if hasattr(m, "tool_calls") and m.tool_calls:
                for tc in m.tool_calls:
                    if tc.get("id") == msg.tool_call_id:
                        args_asli = tc.get("args", {})
                        break
                if args_asli:
                    break

        update.update(
            self._skills.simpan_skill(
                msg.name, args_asli, current_task_desc, current_skill_trace,
                current_rag_candidates_trace,
            )
        )
        return update

    # ==========================================
    # --- Entry point utama ---
    # ==========================================
    def _orchestrator(self, state: AgentState) -> dict:
        messages_raw = list(state.get("messages", []))
        messages = self._bersihkan_pesan_ai_kosong(messages_raw)

        pending_tasks = state.get("pending_tasks", "")
        current_summary = state.get("summary", "")
        revision_count = state.get("revision_count", 0)
        tool_repeat_count = state.get("tool_repeat_count", 0)
        last_tool_signature = state.get("last_tool_signature", "")
        last_tool_names = state.get("last_tool_names", "")
        current_task_desc = state.get("current_task_desc", "")
        current_task_desc_full = state.get("current_task_desc_full", "")
        current_skill_trace = state.get("current_skill_trace", [])
        current_rag_candidates_trace = state.get("current_rag_candidates_trace", [])
        baru_saja_tutup_task = state.get("baru_saja_tutup_task", False)
        tools_dipaksa_manual = state.get("tools_dipaksa_manual", [])
        mode_eksplorasi_tersimpan = state.get("mode_eksplorasi", None)
        id_pesan_task_aktif = state.get("id_pesan_task_aktif", None)
        gorilla_aktif_override = state.get("gorilla_aktif_override", None)
        id_toolmsg_reward_terproses = state.get("id_toolmsg_reward_terproses", None)  # [BARU]

        update_hitl_reward = self._cek_hasil_hitl_reward(
            messages_raw, current_task_desc, current_skill_trace,
            current_rag_candidates_trace, id_toolmsg_reward_terproses,
            id_pesan_task_aktif_lama=id_pesan_task_aktif,
        )

        if "current_task_desc" in update_hitl_reward:
            current_task_desc = update_hitl_reward["current_task_desc"]
        if "current_task_desc_full" in update_hitl_reward:
            current_task_desc_full = update_hitl_reward["current_task_desc_full"]
        if "current_skill_trace" in update_hitl_reward:
            current_skill_trace = update_hitl_reward.get("current_skill_trace") or []
        if "current_rag_candidates_trace" in update_hitl_reward:
            current_rag_candidates_trace = update_hitl_reward.get("current_rag_candidates_trace") or []
        if "id_pesan_task_aktif" in update_hitl_reward:
            id_pesan_task_aktif = update_hitl_reward["id_pesan_task_aktif"]

        # 1. System prompt statis (KV-cache)
        messages = self._pasang_system_prompt(messages)

        # 2. Anchor task baru -- DIMAJUKAN ke sini (sync/murah, cuma butuh
        # messages_raw) supaya current_task_desc SUDAH siap SEBELUM query
        # Tool-RAG disusun & semua retrieval I/O ditembak paralel di bawah.
        (task_desc_baru, current_task_desc, current_task_desc_full,
        id_pesan_task_aktif, human_msg_lengkap_untuk_rag) = self._deteksi_task_baru(
            messages_raw, current_skill_trace, current_task_desc,
            current_task_desc_full, id_pesan_task_aktif,
        )

        # 3. Query Tool-RAG (sync, murni string-building) -- disusun sedekat
        # mungkin dengan kondisi TERKINI, SEBELUM ditembak ke retrieval paralel.
        query_rag = self._tools.bangun_query(
            messages_raw, current_skill_trace, current_task_desc, current_task_desc_full,
            baru_saja_tutup_task,
        )

        # 4. [HARDENING KONKURENSI] Ringkas memori + cari skill sukses/gagal +
        # retrieval Tool-RAG dijalankan BERSAMAAN lewat thread-pool, bukan
        # satu-satu berurutan -- lihat _jalankan_io_paralel.
        hasil_io = self._jalankan_io_paralel(
            messages=messages,
            current_summary=current_summary,
            current_task_desc=current_task_desc,
            query_rag=query_rag,
            gorilla_aktif_override=gorilla_aktif_override,
        )
        messages_dioptimalkan = hasil_io["cleaned_messages"]
        ringkasan_baru = hasil_io["ringkasan_baru"]

        # 5. Reminder sementara + konteks skill library (mode eksplorasi, dst.)
        # dirakit dari hasil retrieval paralel di atas.
        messages_dioptimalkan = self._tambahkan_reminder(
            messages_dioptimalkan, pending_tasks, revision_count, tool_repeat_count, last_tool_names
        )
        skill_ctx = self._skills.rakit_context(
            current_task_desc, mode_eksplorasi_tersimpan,
            hasil_io["skills_sukses"], hasil_io["skills_gagal"],
        )
        messages_dioptimalkan = messages_dioptimalkan + skill_ctx["messages_tambahan"]

        # 6. Safety-net Jinja "No user query found in messages"
        messages_dioptimalkan = self._pastikan_ada_human_message(
            messages_dioptimalkan, current_task_desc_full, current_task_desc
        )

        # 7. Rakit LLM ter-bind dari hasil retrieval Tool-RAG paralel di atas
        # (termasuk force-injection skill sukses/trace aktif/tool manual).
        llm_untuk_invoke, keputusan_rag_baru = self._tools.rakit_llm(
            query_rag=query_rag,
            tools_relevan=hasil_io["tools_relevan"],
            skills_sukses=skill_ctx["skills_sukses"],
            current_skill_trace=current_skill_trace,
            tools_dipaksa_manual=tools_dipaksa_manual,
        )

        print("\n[Log Sistem] AI Utama sedang menganalisis input atau menyusun jawaban...")
        response = self._invoke_llm_aman(llm_untuk_invoke, messages_dioptimalkan)
        self._log_metrik(response)

        # 8. Susun update_state balasan
        update_state = self._bangun_update_state(
            response=response,
            ringkasan_baru=ringkasan_baru,
            revision_count=revision_count,
            tool_repeat_count=tool_repeat_count,
            last_tool_signature=last_tool_signature,
            #current_task_desc=current_task_desc,
            current_skill_trace=current_skill_trace,
            tools_dipaksa_manual=tools_dipaksa_manual,
            task_desc_baru=task_desc_baru,
            human_msg_lengkap_untuk_rag=human_msg_lengkap_untuk_rag,
            id_pesan_task_aktif=id_pesan_task_aktif,
            mode_eksplorasi_aktif=skill_ctx["mode_eksplorasi_aktif"],
            mode_eksplorasi_baru_diputuskan=skill_ctx["mode_eksplorasi_baru_diputuskan"],
            keputusan_rag_baru=keputusan_rag_baru,
            #current_rag_candidates_trace=current_rag_candidates_trace,
        )

        update_final = {**update_hitl_reward, **update_state}

        pesan_gabungan = update_hitl_reward.get("messages", []) + update_state.get("messages", [])
        if pesan_gabungan:
            update_final["messages"] = pesan_gabungan

        # 9. State Cleaner (SQLite)
        update_final = self._bersihkan_pesan_lama(state, update_final, id_pesan_task_aktif)

        return update_final

    def __call__(self, state: AgentState) -> dict:
        return self._orchestrator(state)

    def close(self) -> None:
        """Matikan thread-pool I/O paralel (`self._io_pool`) secara graceful --
        panggil ini saat proses/app dimatikan (mis. shutdown hook FastAPI),
        BUKAN per giliran. Menunggu semua panggilan yang masih berjalan
        selesai dulu sebelum benar-benar keluar."""
        self._io_pool.shutdown(wait=True)