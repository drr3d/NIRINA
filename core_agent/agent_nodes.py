import operator
import json
import hashlib
import random
from typing import Annotated, TypedDict, Any, Optional

# Import LangChain & LangGraph components
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import add_messages

PREFIX_NUDGE_SISTEM = (
    "[SISTEM", "[INFO SISTEM", "[PERINGATAN SISTEM",
    "[SYSTEM", "[INFO SYSTEM", "[WARNING SYSTEM",
)

def _bukan_nudge_sistem(konten: str) -> bool:
    """True kalau `konten` BUKAN pesan nudge/reminder internal (lihat
    PREFIX_NUDGE_SISTEM) -- dipakai tiap kali kode di sini perlu "pesan human
    TERAKHIR" (task-detection & query Tool-RAG), supaya kalau suatu saat ada
    HumanMessage nudge yang ke-persist ke state (saat ini belum ada di jalur
    single-agent -- reminder di sini semuanya SystemMessage, cuma dipakai lokal
    di messages_dioptimalkan, tidak pernah disimpan ke state -- tapi pola ini
    SUDAH dipakai di jalur multi-agent lewat _giliran_ini di agent_patterns.py,
    jadi guard yang sama disiapkan di sini juga sebagai pengaman proaktif),
    nudge itu tidak pernah salah tangkap jadi instruksi user yang sesungguhnya.
    """
    konten = (konten or "").strip()
    return bool(konten) and not konten.upper().startswith(PREFIX_NUDGE_SISTEM)


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

NAMA_TOOL_META_BUKAN_BAGIAN_TRACE = {
    "tools_batal",
    "atur_gorilla_tool_rag",
    "minta_tool_manual",
    "lupakan_skill_gagal",
    "simpan_catatan_penting",
    "cari_catatan_penting",
    "daftar_ide_catatan",
}

class AgentState(TypedDict):
    """
    Representasi memori sentral untuk AI Agent.
    - messages: Menyimpan riwayat obrolan (ditumpuk).
    - revision_count: Menghitung berapa kali AI sudah direvisi.
    """
    messages: Annotated[list, add_messages]
    revision_count: Annotated[int, operator.add]
    pending_tasks: str # <-- untuk monitoring pending task
    summary: str # <-- storage untuk ringkasan
    # --- Guard pengulangan tool call (lihat _signature_tool_calls di atas) ---
    last_tool_signature: str  # hash nama+args tool call terakhir (utk deteksi ulang persis)
    last_tool_names: str      # buat reminder/log
    tool_repeat_count: Annotated[int, operator.add]  # berapa kali berturut-turut identik

     # ---Skill Library (Voyager-style) ---
    current_task_desc: str                              # diisi user saat kasih task baru (dipotong [:300], khusus skill library)
    current_task_desc_full: str                          # versi UTUH (tidak dipotong), khusus query Tool-RAG
    id_toolmsg_reward_terproses: Optional[str]   # tool_call_id ToolMessage tools_reward/gagal/batal TERAKHIR yang sudah diproses _cek_hasil_hitl_reward -- guard biar nggak diproses ulang tiap giliran selama ToolMessage-nya masih nangkring di riwayat
    id_pesan_task_aktif: Optional[str]                    # id HumanMessage anchor task ini -- dilindungi dari State Cleaner selama task masih aktif
    baru_saja_tutup_task: bool  # True HANYA utk giliran PERTAMA setelah task ditutup via tools_reward/tools_gagal/tools_batal -- dipakai _bangun_query_rag utk cegah "Konteks terakhir" ikut narik teks penutup task yang sudah closed. Direset ke False lagi di giliran berikutnya.
    mode_eksplorasi: Optional[bool]                       # diputuskan SEKALI di awal task -- True = referensi skill sukses SENGAJA disembunyikan (dorong eksplorasi jalur baru)
    gorilla_aktif_override: Optional[bool]                # toggle Tool-RAG PER-SESI (None = ikut default instance/config, True/False = override percakapan ini doang -- lihat catatan di __init__)
    current_skill_trace: Annotated[list,  replace_atau_tambah]   # numpuk selama task berjalan
    tools_dipaksa_manual: Annotated[list, replace_atau_tambah]   # nama tool yang diminta AI lewat sinyal 'minta_tool_manual' -- lihat GorillaToolSelector.proses_permintaan_tool_manual
    current_rag_candidates_trace: Annotated[list, replace_atau_tambah]  # riwayat kandidat Tool-RAG (query + tools + asal-usulnya) per giliran, sepanjang task aktif -- lihat GorillaToolSelector.pilih_llm & SkillLibrary.simpan_skill

# ==========================================
# --- 2. SUB-KOMPONEN SPESIALISASI ---
# ==========================================
class SkillLibraryOrchestrator:
    """
    Membungkus semua interaksi dengan SkillLibrary (Voyager-style):
      - Cari skill relevan (sukses & gagal) untuk task yang sedang berjalan.
      - Putuskan (atau lanjutkan keputusan lama) mode eksplorasi vs eksploitasi.
      - Format keduanya jadi pesan yang disisipkan ke prompt.
      - Simpan skill baru saat trace task selesai (reward/gagal), atau buang
        jejaknya saat task dibatalkan (tools_batal).

    Kalau `skill_library` None, instance ini otomatis jadi no-op (`.aktif`
    False) -- AIBrainProcessor tidak perlu cek None di banyak tempat lagi.
    """

    def __init__(
        self,
        skill_library: Any = None,
        top_k_skill: int = 3,
        maks_umur_skill_gagal_detik: Optional[float] = 2 * 24 * 3600,
        min_similarity_skill_sukses: float = 0.80,
        min_similarity_skill_gagal: float = 0.65,
        ambang_similarity_tinggi: float = 0.85,   # skor>=90 HARUS dibarengi similarity setinggi ini baru 0% eksplorasi
        ambang_similarity_rendah: float = 0.75,   # di bawah ini, similarity terlalu lemah -> WAJIB eksplorasi apapun skor-nya
        min_skor_toexplore: int = 80,
        max_skor_toexplore: int = 90,
        probabilitas_perskill_desccutoff: int = 100,
    ):
        self.skill_library = skill_library
        self.top_k_skill = top_k_skill
        self.maks_umur_skill_gagal_detik = maks_umur_skill_gagal_detik
        self.min_similarity_skill_sukses = min_similarity_skill_sukses
        self.min_similarity_skill_gagal = min_similarity_skill_gagal
        self.AMBANG_SIMILARITY_TINGGI = ambang_similarity_tinggi
        self.AMBANG_SIMILARITY_RENDAH = ambang_similarity_rendah
        self.min_skor_toexplore = min_skor_toexplore
        self.max_skor_toexplore = max_skor_toexplore
        self.probabilitas_perskill_desccutoff = probabilitas_perskill_desccutoff

    @property
    def aktif(self) -> bool:
        return self.skill_library is not None

    def _probabilitas_untuk_skill(self, s: dict) -> float:
        # Voyager pada umumnya ketika sudah mendapatkan path tools yang sesuai dengan task,
        #  jika kembali diberikan task yang sama, maka kemungkinan besar hampir pasti tidak akan mencari(explore)
        #  path tools yang lebih efisien. Dengan menerapkan metoda dibawah ini, diharapkan Agent bisa mencari
        #  path yang lebih efisien.
        # Contoh, Task `konek ke internet`` kasus-1, mungkin percobaan pertama Agent akan mengambil 4 langkah,
        #           padahal untuk kasus-1 itu aslinya hanya butuh 2 langkah, jika tidak implement metoda  tambahan
        #  seperti dibawah, akan sangat kecil kemungkinan Agent akan memperoleh path yang sempurna.
        skor = s.get("skor", 0)
        sim = s.get("similarity", 0)
        if skor < self.min_skor_toexplore or sim < self.AMBANG_SIMILARITY_RENDAH:
            return 1.0
        if skor >= self.max_skor_toexplore and sim >= self.AMBANG_SIMILARITY_TINGGI:
            return 0.0
        return 0.5

    def siapkan_context(self, current_task_desc: str, mode_eksplorasi_tersimpan: Optional[bool]) -> dict:
        """
        Return dict:
          - messages_tambahan: pesan (HumanMessage) yang perlu ditempel ke ekor
            messages_dioptimalkan (list kosong kalau skill library nonaktif
            atau tidak ada skill relevan).
          - mode_eksplorasi_aktif / mode_eksplorasi_baru_diputuskan: bool.
          - skills_sukses / skills_gagal: dipakai lagi oleh GorillaToolSelector
            buat memaksa tool dari skill sukses ikut ter-bind meski Tool-RAG
            melewatkannya.
        """
        default = {
            "messages_tambahan": [],
            "mode_eksplorasi_aktif": False,
            "mode_eksplorasi_baru_diputuskan": False,
            "skills_sukses": [],
            "skills_gagal": [],
        }
        if not (self.aktif and current_task_desc):
            return default

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

        mode_eksplorasi_aktif = False
        mode_eksplorasi_baru_diputuskan = False

        if mode_eksplorasi_tersimpan is not None:
            # Sudah pernah diputuskan sebelumnya di task ini -- pakai apa
            # adanya, JANGAN di-roll ulang (biar konsisten sepanjang task).
            mode_eksplorasi_aktif = mode_eksplorasi_tersimpan
        elif skills_sukses:
            probabilitas_per_skill = [
                (s["deskripsi"][:self.probabilitas_perskill_desccutoff],
                 s.get("skor", 0), s.get("similarity", 0),
                 self._probabilitas_untuk_skill(s))
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

        teks_skill = self.skill_library.format_untuk_prompt(skills_sukses, skills_gagal)

        messages_tambahan = []
        if teks_skill:
            messages_tambahan.append(HumanMessage(content=f"[INFO SISTEM]\n{teks_skill}"))
            messages_tambahan.append(HumanMessage(content=(
                "[PENGINGAT PRIORITAS]\n"
                "Blok skill library di atas HANYALAH latar belakang historis, "
                "BUKAN instruksi untuk sekarang. Yang WAJIB kamu ikuti adalah "
                "instruksi eksplisit dari pesan user SEBELUMNYA di percakapan "
                "ini -- kalau urutan langkah atau tool yang diminta user berbeda "
                "dari referensi skill library, ABAIKAN referensi itu sepenuhnya "
                "dan ikuti instruksi user apa adanya."
            )))

        return {
            "messages_tambahan": messages_tambahan,
            "mode_eksplorasi_aktif": mode_eksplorasi_aktif,
            "mode_eksplorasi_baru_diputuskan": mode_eksplorasi_baru_diputuskan,
            "skills_sukses": skills_sukses,
            "skills_gagal": skills_gagal,
        }

    @staticmethod
    def reset_task_state() -> dict:
        """State reset yang dipakai tiap kali sebuah task dianggap TUNTAS
        (reward/gagal tersimpan) ATAU DIBATALKAN (tools_batal)."""
        return {
            "current_skill_trace": None,
            "current_task_desc": "",
            "current_task_desc_full": "",
            "mode_eksplorasi": None,
            "id_pesan_task_aktif": None,
            "current_rag_candidates_trace": None,
            "baru_saja_tutup_task": True,  #
        }

    def simpan_skill(
        self,
        nama_tool: str,
        args: dict,
        current_task_desc: str,
        current_skill_trace: list,
        current_rag_candidates_trace: Optional[list] = None,
    ) -> dict:
        
        if not self.aktif:
            return {}

        if not current_skill_trace:
            # Cegah double-save jika trace sudah kosong
            print(f"[Skill Library] Abaikan {nama_tool} karena trace kosong (Double call).")
            return self.reset_task_state()

        status = "berhasil" if nama_tool == "tools_reward" else "gagal"
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
            rag_candidates_trace=current_rag_candidates_trace or [],
        )
        return self.reset_task_state()

class GorillaToolSelector:
    def __init__(
        self,
        tool_registry: Any,
        tools_fallback: list,
        llm_mentah: Any,
        top_k_tools: int = 8,
        max_ragquery_lstcontxtcutoff: int = 1000,
        maks_tool_dipaksa_manual: int = 6,
    ):
        self.tool_registry = tool_registry
        self.tools_fallback = tools_fallback
        self.llm_mentah = llm_mentah
        self.top_k_tools = top_k_tools
        self.max_ragquery_lstcontxtcutoff = max_ragquery_lstcontxtcutoff
        self.maks_tool_dipaksa_manual = maks_tool_dipaksa_manual

        self.aktif_default = tool_registry is not None

    @property
    def nama_semua_tool(self) -> set:
        """Nama SEMUA tool yang genuinely ada untuk agent ini (dari
        `tools_fallback`, list lengkap yang dikirim saat AIBrainProcessor
        dibentuk) -- dipakai buat validasi permintaan minta_tool_manual,
        BUKAN dari hasil retrieval RAG yang cuma subset."""
        return {t.name for t in self.tools_fallback}

    def _bangun_query_rag(
        self, messages_raw, current_skill_trace, current_task_desc, current_task_desc_full,
        baru_saja_tutup_task: bool = False,
    ) -> str:
        # Gunakan pesan terkini agar RAG tidak nyangkut
        pesan_human_terbaru = ""
        for m in reversed(messages_raw):
            if m.type == "human" and _bukan_nudge_sistem(m.content):
                pesan_human_terbaru = m.content.strip()
                break
        if not pesan_human_terbaru:
            pesan_human_terbaru = current_task_desc_full or current_task_desc

        konteks_terkini = ""
        if current_skill_trace:  # enhance on mid-task
            for m in reversed(messages_raw):
                if m.type in ("ai", "tool") and (m.content or "").strip():
                    konteks_terkini = f"Konteks terakhir: {m.content.strip()[:self.max_ragquery_lstcontxtcutoff]}"
                    break
        elif (
            not baru_saja_tutup_task
            and len(messages_raw) >= 2 and messages_raw[-2].type == "ai" and _bukan_nudge_sistem(messages_raw[-2].content)
        ):
            konteks_terkini = f"Konteks terakhir: {messages_raw[-2].content.strip()[:self.max_ragquery_lstcontxtcutoff]}"

        query_rag = " ".join(filter(None, [pesan_human_terbaru, konteks_terkini])).strip()
        if not query_rag:
            query_rag = current_task_desc_full or current_task_desc
        return query_rag

    def _paksa_masuk(self, tools_relevan: list, rag_tool_names: set, nama_tool_wajib: set, label_log: str) -> None:
        tools_kurang = nama_tool_wajib - rag_tool_names
        if not tools_kurang:
            return
        for tool in self.tools_fallback:
            if tool.name in tools_kurang and tool.name not in rag_tool_names:
                tools_relevan.append(tool)
                rag_tool_names.add(tool.name)
                print(f"{label_log}: '{tool.name}'")

    def proses_permintaan_tool_manual(self, nama_diminta: str, tools_dipaksa_manual_sebelumnya: list) -> dict:
        """
        Handle sinyal tool 'minta_tool_manual' (lihat
        AIBrainProcessor._proses_sinyal_tool_khusus & contoh registrasi tool
        aslinya di plugin_minta_tool_manual.py). Validasi nama tool terhadap
        `nama_semua_tool` (daftar tool ASLI, bukan subset RAG) SEBELUM
        dipersist ke state -- soalnya kalau nama hasil typo/halusinasi LLM
        sampai kebobolan masuk `tools_dipaksa_manual`, entry itu akan numpuk
        SELAMANYA sepanjang task (di-scan ulang tiap giliran lewat
        pilih_llm(), tapi tidak akan pernah match tool apapun -- cuma jadi
        sampah state).

        Return dict update_state PARSIAL:
          - {} kalau ditolak (nama kosong/tidak ditemukan/sudah pernah
            diminta/sudah kena batas maks_tool_dipaksa_manual) -- TIDAK ADA
            perubahan state sama sekali.
          - {"tools_dipaksa_manual": [nama_diminta]} kalau diterima -- list
            berisi HANYA 1 item baru (reducer `replace_atau_tambah` di
            AgentState yang menggabungkannya dengan yang sudah ada, pola
            yang sama dengan current_skill_trace).
        """
        nama_diminta = (nama_diminta or "").strip()
        sudah_ada = tools_dipaksa_manual_sebelumnya or []

        if not nama_diminta:
            return {}

        if nama_diminta not in self.nama_semua_tool:
            print(
                f"\n[🧩 Tool Manual Override] '{nama_diminta}' TIDAK DITEMUKAN "
                f"di daftar tool yang tersedia sama sekali -- diabaikan."
            )
            return {}

        if nama_diminta in sudah_ada:
            print(
                f"\n[🧩 Tool Manual Override] '{nama_diminta}' sudah pernah "
                f"diminta sebelumnya di task ini -- tidak diduplikasi."
            )
            return {}

        if len(sudah_ada) >= self.maks_tool_dipaksa_manual:
            print(
                f"\n[🧩 Tool Manual Override] Sudah mencapai batas "
                f"{self.maks_tool_dipaksa_manual} tool manual untuk task ini "
                f"-- permintaan '{nama_diminta}' diabaikan."
            )
            return {}

        print(
            f"\n[🧩 Tool Manual Override] '{nama_diminta}' DITEMUKAN -- akan "
            f"dipaksa ikut ter-bind mulai giliran BERIKUTNYA untuk sisa task ini."
        )
        return {"tools_dipaksa_manual": [nama_diminta]}

    @staticmethod
    def reset_tool_manual() -> dict:
        """Reset di batas task -- dipanggil BARENGAN
        SkillLibraryOrchestrator.reset_task_state() di
        AIBrainProcessor._proses_sinyal_tool_khusus (saat tools_batal atau
        tools_reward/tools_gagal). Override manual ini SENGAJA task-scoped,
        BUKAN permanen sepanjang sesi -- supaya task baru mulai dari hasil
        Tool-RAG bersih lagi, tidak numpuk override dari task-task
        sebelumnya yang sudah tidak relevan."""
        return {"tools_dipaksa_manual": None}

    def pilih_llm(
        self,
        *,
        messages_raw: list,
        current_skill_trace: list,
        current_task_desc: str,
        current_task_desc_full: str,
        gorilla_aktif_override: Optional[bool],
        skills_sukses: list,
        tools_dipaksa_manual: Optional[list] = None,
        baru_saja_tutup_task: bool = False,
    ):
        """Return tuple (llm_terbind, keputusan_rag):
          - llm_terbind: LLM yang sudah di-bind_tools() dengan subset tool
            yang relevan (atau SEMUA tool fallback kalau Tool-RAG nonaktif).
          - keputusan_rag: dict snapshot PERSIS apa yang dicetak di baris log
            "[🦍 Tool-RAG Gorilla]" di bawah (query + daftar tool final yang
            dipilih) -- dinumpuk ke current_rag_candidates_trace, ATAU None
            kalau Tool-RAG nonaktif (tidak ada "keputusan filter" yang perlu
            direkam saat semua tool langsung dibind apa adanya)."""
        aktif_efektif = self.aktif_default if gorilla_aktif_override is None else gorilla_aktif_override

        if self.tool_registry is None or not aktif_efektif:
            return self.llm_mentah.bind_tools(self.tools_fallback), None

        query_rag = self._bangun_query_rag(
            messages_raw, current_skill_trace, current_task_desc, current_task_desc_full,
            baru_saja_tutup_task,
        )

        tools_relevan = (
            self.tool_registry.get_relevant_tools(query_rag, top_k=self.top_k_tools)
            if query_rag else list(self.tools_fallback)
        )
        rag_tool_names = {t.name for t in tools_relevan}

        # INJEKSI PAKSA TOOL DARI SKILL LIBRARY SUKSES
        if skills_sukses:
            skill_tool_names = {
                trace.get("name")
                for s in skills_sukses
                for trace in s.get("trace", [])
                if trace.get("name")
            }
            self._paksa_masuk(
                tools_relevan, rag_tool_names, skill_tool_names,
                "[🔧 Skill Injector] Memaksa masuk tool dari masa lalu",
            )

        # INJEKSI PAKSA TOOL YANG SEDANG DIPAKAI (CURRENT TRACE)
        if current_skill_trace:
            active_tool_names = {tc.get("name") for tc in current_skill_trace if tc.get("name")}
            self._paksa_masuk(
                tools_relevan, rag_tool_names, active_tool_names,
                "[🔒 Tool Lock] Mengunci tool yang sedang dipakai di task ini",
            )

        # INJEKSI PAKSA TOOL YANG DIMINTA MANUAL OLEH AI
        # (lihat proses_permintaan_tool_manual -- nama di sini sudah
        # divalidasi SEBELUM dipersist ke state, jadi di sini tinggal
        # dipercaya apa adanya).
        if tools_dipaksa_manual:
            self._paksa_masuk(
                tools_relevan, rag_tool_names, set(tools_dipaksa_manual),
                "[🧩 Tool Manual Override] Menambahkan tool yang diminta manual",
            )

        nama_tools_final = [t.name for t in tools_relevan]
        print(
            f"\n[🦍 Tool-RAG Gorilla] Query: \"{query_rag[:120]}\" -> "
            f"{len(tools_relevan)} tool dipilih dari {len(self.tools_fallback)}: "
            f"{nama_tools_final}"
        )

        keputusan_rag = {
            "query_rag": query_rag[:300],
            "total_tools_tersedia": len(self.tools_fallback),
            "tools_dipilih": nama_tools_final,
        }
        return self.llm_mentah.bind_tools(tools_relevan), keputusan_rag


def hitung_perintah_hapus_pesan_lama(semua_pesan_asli: list, anchor_id: Optional[str], batas_simpan_db: int) -> list:
    """
    [STATE CLEANER] Hitung daftar RemoveMessage untuk pesan yang sudah
    'usang' (di luar `batas_simpan_db` pesan terakhir) supaya LangGraph
    menghapusnya dari checkpointer (SQLite) begitu sesi lama di-load ulang
    -- TIDAK BOLEH ditarik semua ke RAM. Ini beda lapisan sama sekali dari
    `optimasi_konteks_langchain` (itu ngatur apa yang dikirim ke LLM,
    fungsi ini ngatur apa yang disimpan permanen di disk).

    Selalu melindungi (tidak pernah menghapus):
      - `anchor_id`: id HumanMessage anchor task yang MASIH AKTIF, walau
        posisinya di luar window N-pesan-terakhir.
      - HumanMessage ASLI (bukan nudge sistem) paling baru di seluruh riwayat.
    """
    if len(semua_pesan_asli) <= batas_simpan_db:
        return []

    id_pesan_dilindungi = set()
    if anchor_id:
        id_pesan_dilindungi.add(anchor_id)
    for m in reversed(semua_pesan_asli):
        if getattr(m, "type", None) == "human" and _bukan_nudge_sistem(getattr(m, "content", "")):
            if getattr(m, "id", None):
                id_pesan_dilindungi.add(m.id)
            break

    pesan_usang = semua_pesan_asli[:-batas_simpan_db]
    if id_pesan_dilindungi:
        pesan_usang = [m for m in pesan_usang if getattr(m, "id", None) not in id_pesan_dilindungi]

    return [RemoveMessage(id=msg.id) for msg in pesan_usang if msg.id]


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

        batas_simpan_db: int = 10,
        max_humanmsgs_taskdesccutoff: int = 1000,
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
        self.enable_optimization = enable_optimization
        self.batas_pesan_inturn = batas_pesan_inturn
        self.batas_karakter_inturn = batas_karakter_inturn
        self.panjang_min_kompresi = panjang_min_kompresi
        self.batas_simpan_db = batas_simpan_db
        self.max_humanmsgs_taskdesccutoff = max_humanmsgs_taskdesccutoff

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
    # --- Langkah 2: optimasi konteks & reminder sementara ---
    # ==========================================
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

    def _proses_sinyal_tool_khusus(self, update_state, response, current_task_desc, current_skill_trace,
                                    tools_dipaksa_manual, current_rag_candidates_trace=None):
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

            # --- TAMBAHAN UNTUK RESET/BATAL ---
            """
            if nama_tool == "tools_batal":
                print("[Skill Library] 🧹 Membatalkan dan mereset jejak task yang menggantung.")
                update_state.update(SkillLibraryOrchestrator.reset_task_state())
                update_state.update(GorillaToolSelector.reset_tool_manual())
                continue

            if nama_tool in ("tools_reward", "tools_gagal") and self.skill_library:
                update_state.update(
                    self._skills.simpan_skill(
                        nama_tool, args, current_task_desc, current_skill_trace,
                        current_rag_candidates_trace,
                    )
                )
                update_state.update(GorillaToolSelector.reset_tool_manual())
            """

    def _bangun_update_state(
        self, *, response, ringkasan_baru, revision_count,
        tool_repeat_count, last_tool_signature,
        current_task_desc, current_skill_trace, tools_dipaksa_manual,
        task_desc_baru, human_msg_lengkap_untuk_rag, id_pesan_task_aktif,
        mode_eksplorasi_aktif, mode_eksplorasi_baru_diputuskan,
        keputusan_rag_baru=None, current_rag_candidates_trace=None,
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
            update_state["current_skill_trace"] = tool_calls_relevan  # numpuk via operator.add

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
        self._proses_sinyal_tool_khusus(
            update_state, response, current_task_desc, current_skill_trace, tools_dipaksa_manual,
            current_rag_candidates_trace,
        )

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
            # Gabungkan perintah hapus ke dalam array messages yang akan
            # di-update -- LangGraph akan membaca RemoveMessage ini dan
            # menghapusnya dari SQLite!
            update_state["messages"] = perintah_hapus + update_state["messages"]
            print(f"\n[🧹 State Cleaner] Menginstruksikan SQLite untuk menghapus {len(perintah_hapus)} pesan usang dari memori hard disk!")

        return update_state

    def _cek_hasil_hitl_reward(self, messages_raw, current_task_desc, current_skill_trace,
                            current_rag_candidates_trace, id_toolmsg_reward_terproses):
        """Cek apakah pesan TERAKHIR di riwayat itu ToolMessage jawaban dari
        tools_reward/tools_gagal/tools_batal yang BELUM diproses. Kalau iya:
        - Kalau isinya SYSTEM ABORT -> user batalin, JANGAN simpan apa-apa,
            JANGAN reset task (biarkan task tetap jalan, AI bisa lanjut/ralat).
        - Kalau isinya sinyal asli dari tool -> BARU sekarang simpan_skill()
            dipanggil & task di-reset -- karena ini titik di mana approval-nya
            sudah pasti clear.
        id_toolmsg_reward_terproses: tool_call_id terakhir yang SUDAH diproses --
        guard biar nggak diproses ulang tiap giliran (ToolMessage-nya tetap
        nangkring di riwayat selama beberapa turn ke depan).
        """
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
        """
        Entry point yang dieksekusi oleh LangGraph. Setiap langkah
        didelegasikan ke method/kelas spesialisasinya masing-masing:
        0. [BARU] Cek apakah giliran SEBELUMNYA ada tools_reward/gagal/batal
            yang baru kejawab (disetujui/dibatalkan via HITL) -- proses efek
            sampingnya (simpan_skill/reset) DI SINI, bukan pas AI manggilnya.
        1. Bersihkan sampah pesan & pasang system prompt statis (KV-cache).
        2. Optimasi/kompresi konteks + tempel reminder sementara.
        3. Deteksi anchor task baru (dipakai skill library & Tool-RAG).
        4. Siapkan konteks skill library (retrieval, mode eksplorasi).
        5. Safety-net: pastikan selalu ada HumanMessage di prompt.
        6. Pilih tool relevan (Tool-RAG Gorilla, termasuk tool manual
            override dari minta_tool_manual) lalu panggil LLM.
        7. Susun update_state balasan (trace, task desc, guard, dst).
        8. Bersihkan pesan usang dari SQLite (state cleaner).
        """
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

        # 0. [BARU] Cek hasil approval HITL utk tools_reward/gagal/batal giliran
        # lalu -- simpan_skill() beneran BARU dipanggil DI SINI kalau disetujui,
        # SKIP total kalau dibatalkan (lihat _cek_hasil_hitl_reward).
        update_hitl_reward = self._cek_hasil_hitl_reward(
            messages_raw, current_task_desc, current_skill_trace,
            current_rag_candidates_trace, id_toolmsg_reward_terproses,
        )
        # Terapkan ke variabel lokal SEBELUM langkah-langkah berikutnya, supaya
        # _deteksi_task_baru dkk sudah lihat versi current_task_desc/
        # current_skill_trace yang ter-update (bukan versi basi dari state lama).
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

        # 1. System prompt statis + 2. optimasi konteks + reminder
        messages = self._pasang_system_prompt(messages)
        messages_dioptimalkan, ringkasan_baru = self._optimasi_konteks(messages, current_summary)
        messages_dioptimalkan = self._tambahkan_reminder(
            messages_dioptimalkan, pending_tasks, revision_count, tool_repeat_count, last_tool_names
        )

        # 3. Anchor task baru
        (task_desc_baru, current_task_desc, current_task_desc_full,
        id_pesan_task_aktif, human_msg_lengkap_untuk_rag) = self._deteksi_task_baru(
            messages_raw, current_skill_trace, current_task_desc,
            current_task_desc_full, id_pesan_task_aktif,
        )

        # 4. Skill library: retrieval + mode eksplorasi + pesan tambahan
        skill_ctx = self._skills.siapkan_context(current_task_desc, mode_eksplorasi_tersimpan)
        messages_dioptimalkan = messages_dioptimalkan + skill_ctx["messages_tambahan"]

        # 5. Safety-net Jinja "No user query found in messages"
        messages_dioptimalkan = self._pastikan_ada_human_message(
            messages_dioptimalkan, current_task_desc_full, current_task_desc
        )

        # 6. Tool-RAG Gorilla (dipanggil TEPAT SEBELUM invoke supaya query-nya
        # sedekat mungkin dengan kondisi TERKINI) + panggil LLM
        llm_untuk_invoke, keputusan_rag_baru = self._tools.pilih_llm(
            messages_raw=messages_raw,
            current_skill_trace=current_skill_trace,
            current_task_desc=current_task_desc,
            current_task_desc_full=current_task_desc_full,
            gorilla_aktif_override=gorilla_aktif_override,
            skills_sukses=skill_ctx["skills_sukses"],
            tools_dipaksa_manual=tools_dipaksa_manual,
            baru_saja_tutup_task=baru_saja_tutup_task,
        )

        print("\n[Log Sistem] AI Utama sedang menganalisis input atau menyusun jawaban...")
        response = self._invoke_llm_aman(llm_untuk_invoke, messages_dioptimalkan)
        self._log_metrik(response)

        # 7. Susun update_state balasan
        update_state = self._bangun_update_state(
            response=response,
            ringkasan_baru=ringkasan_baru,
            revision_count=revision_count,
            tool_repeat_count=tool_repeat_count,
            last_tool_signature=last_tool_signature,
            current_task_desc=current_task_desc,
            current_skill_trace=current_skill_trace,
            tools_dipaksa_manual=tools_dipaksa_manual,
            task_desc_baru=task_desc_baru,
            human_msg_lengkap_untuk_rag=human_msg_lengkap_untuk_rag,
            id_pesan_task_aktif=id_pesan_task_aktif,
            mode_eksplorasi_aktif=skill_ctx["mode_eksplorasi_aktif"],
            mode_eksplorasi_baru_diputuskan=skill_ctx["mode_eksplorasi_baru_diputuskan"],
            keputusan_rag_baru=keputusan_rag_baru,
            current_rag_candidates_trace=current_rag_candidates_trace,
        )

        update_final = {**update_hitl_reward, **update_state}

        # 8. State Cleaner (SQLite)
        update_final = self._bersihkan_pesan_lama(state, update_final, id_pesan_task_aktif)

        return update_final

    def __call__(self, state: AgentState) -> dict:
        return self._orchestrator(state)