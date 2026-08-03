import operator
from typing import Annotated, TypedDict, Any

# Import LangChain & LangGraph components
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import add_messages

# ==========================================
# --- 0. FUNGSI SUMMARIZER (LLM KECIL) ---
# ==========================================
def buat_ringkasan_memori(pesan_lama: list, fast_llm: Any, ringkasan_sebelumnya: str = "") -> str:
    """Menggunakan LLM sekunder yang cepat untuk meringkas obrolan usang."""
    teks_obrolan = ""
    for p in pesan_lama:
        peran = "User" if isinstance(p, HumanMessage) else "AI"
        if p.content: # Kadang AI manggil tool tanpa teks, kita ambil teksnya saja
            teks_obrolan += f"{peran}: {p.content}\n"
            
    # Jika tidak ada teks untuk diringkas (misal cuma tool call kosong), lewati
    if not teks_obrolan.strip():
        return ringkasan_sebelumnya

    prompt = ChatPromptTemplate.from_messages([
        ("system", 
         "Kamu adalah asisten memori internal AI. Tugasmu meringkas percakapan lama. "
         "Pertahankan instruksi teknis, fakta, atau keputusan penting. "
         "Gabungkan dengan ringkasan sebelumnya secara mulus.\n\n"
         "Ringkasan Sebelumnya:\n{ringkasan_sebelumnya}"
        ),
        ("user", "Rangkum obrolan berikut:\n\n{obrolan}")
    ])
    
    # Langsung jalankan chain
    hasil = (prompt | fast_llm).invoke({"ringkasan_sebelumnya": ringkasan_sebelumnya, "obrolan": teks_obrolan})
    return hasil.content

def optimasi_konteks_langchain(messages, current_summary="", fast_llm=None):
    """
    Optimasi berbasis 'Batas Giliran' (Turn Boundary) -- pengganti sliding-window
    lama yang berbasis jarak-dari-ujung list.
    """
    from langchain_core.messages import ToolMessage

    BATAS_PESAN_AMAN_DALAM_GILIRAN = 24  # Up from 8, if you've more vram on you gpu you, can increase this
    PANJANG_MIN_UNTUK_KOMPRESI = 300 # just remember to adjust your num_ctx

    # 1. Cari index HumanMessage TERAKHIR -> penanda mulainya giliran aktif.
    #    Pesan di idx < last_human_idx berarti berasal dari giliran yg sudah selesai.
    last_human_idx = 0
    for idx, msg in enumerate(messages):
        if msg.type == "human":
            last_human_idx = idx

    total_msgs = len(messages)
    cleaned_messages = []
    pesan_untuk_diringkas = []

    for idx, msg in enumerate(messages):
        if msg.type == "system":
            cleaned_messages.append(msg)
            continue

        is_giliran_selesai = idx < last_human_idx

        # --- A. LOGIKA TOOL MESSAGE (Utuh dari versi Anda) ---
        if msg.type == "tool":
            long_context_inturn = (not is_giliran_selesai and (total_msgs - idx) > BATAS_PESAN_AMAN_DALAM_GILIRAN)
            harus_dikompres = ((is_giliran_selesai or long_context_inturn) and len(msg.content) > PANJANG_MIN_UNTUK_KOMPRESI)

            if harus_dikompres:
                cleaned_messages.append(ToolMessage(
                    content=f"[Log memori: Data teknis '{msg.name}' dikompresi. Jika butuh, panggil ulang tool-nya.]",
                    name=msg.name,
                    tool_call_id=msg.tool_call_id
                ))
            else:
                cleaned_messages.append(msg)
            continue

        # --- B. LOGIKA HUMAN/AI MESSAGE (Dibersihkan & Diringkas) ---
        if is_giliran_selesai and fast_llm:
            if msg.type == "human":
                # Pesan User masuk ringkasan dan DIHAPUS dari HD memory
                pesan_untuk_diringkas.append(msg)
                
            elif msg.type == "ai":
                if getattr(msg, "tool_calls", None):
                    # ⚠️ CRITICAL: Jika AI memanggil tool, JANGAN DIHAPUS dari HD memory!
                    # LangChain butuh pesan ini untuk validasi pasangan ToolMessage.
                    pesan_untuk_diringkas.append(msg) 
                    cleaned_messages.append(msg)      
                else:
                    # Teks AI biasa masuk ringkasan dan DIHAPUS dari HD memory
                    pesan_untuk_diringkas.append(msg)
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

# ==========================================
# --- 2. DEFINISI NODE (KOMPONEN AI) ---
# ==========================================
class AIBrainProcessor:
    """
    Komponen Otak Utama (Brain Node) untuk AI Agent.
    """
    
    def __init__(self, llm_model: Any, tools_list: list, base_prompt: str, fast_llm: Any = None, enable_optimization: bool = True):
        # Binding tools cukup dilakukan sekali saat class dibentuk
        # (Memakai variabel 'llm' dan 'tools' global yang sudah di-import di file ini)
        self.base_prompt = base_prompt
        self._llm_with_tools = llm_model.bind_tools(tools_list)
        self.fast_llm = fast_llm
        self.enable_optimization = enable_optimization # <-- SAKELAR TOGGLE

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
        return SystemMessage(
            content=(
                f"[🚨 PERINGATAN SISTEM: Kamu memiliki instruksi dari user yang masih tertunda:\n"
                f"{pending_tasks}\n"
                f"Segera tindak lanjuti jika user sudah memberikan data yang dibutuhkan!]"
            )
        )

    def _build_retry_reminder(self, percobaan_ke: int) -> SystemMessage:
        return SystemMessage(
            content=(
                f"[⚠️ PERINGATAN SISTEM: Respons kamu di giliran sebelumnya KOSONG "
                f"(percobaan ke-{percobaan_ke}). Lihat kembali hasil tool paling akhir "
                f"di atas, analisis, lalu WAJIB tuliskan jawaban teks akhir untuk user "
                f"SEKARANG. Jangan panggil tool yang sama lagi jika tidak perlu, dan "
                f"jangan kirim respons kosong lagi.]"
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
        messages = list(state.get("messages", []))
        pending_tasks = state.get("pending_tasks", "")
        current_summary = state.get("summary", "") # <-- Ambil ringkasan saat ini

        revision_count = state.get("revision_count", 0) # <-- FIX RETRY KOSONG: hitungan percobaan ulang

        # 1. [OPTIMASI KV-CACHE] System prompt SELALU statis apa adanya (base_prompt murni),
        # tidak pernah lagi disisipi teks dinamis di sini -- lihat penjelasan di _build_pending_reminder.
        if messages and isinstance(messages[0], SystemMessage):
            messages[0] = SystemMessage(content=self.base_prompt)
        else:
            messages.insert(0, SystemMessage(content=self.base_prompt))

        # 2. [TOGGLE MEKANISME OPTIMASI]
        if self.enable_optimization:
            # 2. Pangkas konteks usang (Memakai fungsi global)
            # Mode Cerdas: Pangkas konteks usang & Eksekusi Peringkas
            messages_dioptimalkan, ringkasan_baru = optimasi_konteks_langchain(messages, current_summary, self.fast_llm)
        else:
            # Mode Brutal: Bypass 100%, biarkan memori membengkak apa adanya
            print("\n[⚠️ WARNING] Optimasi Konteks DIMATIKAN. Memori dikirim utuh ke LLM!")
            messages_dioptimalkan = messages
            ringkasan_baru = current_summary

        # 3. [OPTIMASI KV-CACHE] Reminder pending_tasks (kalau ada) ditempel di EKOR list,
        # cuma untuk panggilan invoke() ini -- tidak ikut ke-return/ke-simpan ke state.
        if pending_tasks:
            messages_dioptimalkan = messages_dioptimalkan + [self._build_pending_reminder(pending_tasks)]

        # 3b. [FIX RETRY KOSONG] Kalau ini hasil loop-back dari router karena giliran
        # sebelumnya kosong, tempel reminder ekstra biar model tidak mengulang kesalahan
        # yang sama. Sama seperti reminder lain: cuma untuk invoke() ini, tidak disimpan.
        if revision_count > 0:
            messages_dioptimalkan = messages_dioptimalkan + [self._build_retry_reminder(revision_count)]
        
        print("\n[Log Sistem] AI Utama sedang menganalisis input atau menyusun jawaban...")
        
        # 4. Panggil LLM
        response = self._llm_with_tools.invoke(messages_dioptimalkan)

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
        print("----------------------------------------------\n")
        
        # 6. Siapkan state balasan (Simpan hasil ringkasan agar permanen di DB)
        update_state = {
            "messages": [response],
            "summary": ringkasan_baru 
        }
        
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
        BATAS_SIMPAN_DB = 6 
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