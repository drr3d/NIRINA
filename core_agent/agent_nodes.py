import operator
import random
import re
from typing import Annotated, TypedDict, Any, Optional

# Import LangChain & LangGraph components
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage, RemoveMessage#, SystemMessage
from langgraph.graph.message import add_messages

PREFIX_NUDGE_SISTEM = (
    "[SISTEM", "[INFO SISTEM", "[PERINGATAN SISTEM",
    "[SYSTEM", "[INFO SYSTEM", "[WARNING SYSTEM",
)

_SIMBOL_AWAL_KURUNG = re.compile(r"^\[[^A-Za-z0-9]*")

def _bukan_nudge_sistem(konten: str) -> bool:
    konten = (konten or "").strip()
    if not konten:
        return False
    upper = konten.upper()
    upper_dinormalisasi = _SIMBOL_AWAL_KURUNG.sub("[", upper, count=1)
    return not upper_dinormalisasi.startswith(PREFIX_NUDGE_SISTEM)

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

# ==========================================
# --- 1. ARSITEKTUR CUSTOM STATEGRAPH ---
# ==========================================
def replace_atau_tambah(existing: list, new) -> list:
    if new is None:
        return []          # None = sinyal reset
    return existing + new  # list = nambah

NAMA_TOOL_META_BUKAN_BAGIAN_TRACE = {
    "tools_batal",
    "tools_reward",  
    "tools_gagal",  
    "atur_gorilla_tool_rag",
    "minta_tool_manual",
    "lupakan_skill_gagal",
    "simpan_catatan_penting",
    "cari_catatan_penting",
    "daftar_ide_catatan",
}

NAMA_TOOL_HANYA_UNTUK_PENULIS_VOYAGER = {"tools_reward", "tools_gagal"}

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
    mode_tulis_skill_override: Optional[bool]              # toggle PENULISAN skill library PER-SESI (None = ikut default instance/config, True/False = override percakapan ini doang) -- lihat SkillLibraryOrchestrator.mode_tulis_efektif & sinyal tool 'atur_mode_tulis_skill'. RETRIEVAL/pembacaan skill (cari_sukses/cari_gagal/rakit_context) TIDAK PERNAH dipengaruhi field ini -- selalu aktif apa pun nilainya, supaya siapa pun (bukan cuma yang mengaktifkan mode tulis) tetap dapat manfaat dari skill yang sudah tersimpan.
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
        # --- [BARU] Toggle PENULISAN skill baru, TERPISAH dari retrieval ---
        mode_tulis_skill_default: bool = True,
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

        self.mode_tulis_skill_default = mode_tulis_skill_default

    @property
    def aktif(self) -> bool:
        return self.skill_library is not None

    def mode_tulis_efektif(self, override: Optional[bool]) -> bool:
        return self.mode_tulis_skill_default if override is None else override

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

    def cari_sukses(self, current_task_desc: str) -> list:
        if not (self.aktif and current_task_desc):
            return []
        hasil = self.skill_library.cari_skill_relevan(
            current_task_desc, top_k=self.top_k_skill, status_filter="berhasil",
            min_similarity=self.min_similarity_skill_sukses,
        )
        if hasil:
            print(f"\n [Orchestrator] didapatkan skill sukses: {hasil}")
        return hasil

    def cari_gagal(self, current_task_desc: str) -> list:
        """Sama seperti `cari_sukses`, untuk skill berstatus 'gagal'."""
        if not (self.aktif and current_task_desc):
            return []
        hasil = self.skill_library.cari_skill_relevan(
            current_task_desc, top_k=1, status_filter="gagal",
            maks_umur_detik=self.maks_umur_skill_gagal_detik,
            min_similarity=self.min_similarity_skill_gagal,
        )
        if hasil:
            print(f"\n [Orchestrator] didapatkan skill gagal: {hasil}")
        return hasil

    def rakit_context(
        self, current_task_desc: str, mode_eksplorasi_tersimpan: Optional[bool],
        skills_sukses: list, skills_gagal: list,
    ) -> dict:

        default = {
            "messages_tambahan": [],
            "mode_eksplorasi_aktif": False,
            "mode_eksplorasi_baru_diputuskan": False,
            "skills_sukses": [],
            "skills_gagal": [],
        }
        if not (self.aktif and current_task_desc):
            return default

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

    def siapkan_context(self, current_task_desc: str, mode_eksplorasi_tersimpan: Optional[bool]) -> dict:
        """
        Versi non-paralel (dipertahankan untuk backward-compat/testing) --
        panggil `cari_sukses`/`cari_gagal` berurutan lalu `rakit_context`.
        Jalur utama `_orchestrator` SEKARANG memparalelkan retrieval-nya
        lewat `AIBrainProcessor._jalankan_io_paralel`, tidak lewat sini.
        """
        skills_sukses = self.cari_sukses(current_task_desc)
        skills_gagal = self.cari_gagal(current_task_desc)
        return self.rakit_context(current_task_desc, mode_eksplorasi_tersimpan, skills_sukses, skills_gagal)

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
        mode_tulis_override: Optional[bool] = None,
    ) -> dict:
        
        if not self.aktif:
            return {}

        if not current_skill_trace:
            # Cegah double-save jika trace sudah kosong
            print(f"[Skill Library] Abaikan {nama_tool} karena trace kosong (Double call).")
            return self.reset_task_state()

        if not self.mode_tulis_efektif(mode_tulis_override):
            print(
                f"[Skill Library] ✋ Mode tulis NONAKTIF untuk sesi ini -- "
                f"{nama_tool} disetujui tapi trace task TIDAK disimpan ke "
                f"skill library (cuma ditutup/direset)."
            )
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
        llm_rag: Any = None,

        nama_tools_hanya_tulis: frozenset = frozenset(NAMA_TOOL_HANYA_UNTUK_PENULIS_VOYAGER),
    ):
        self.tool_registry = tool_registry
        self.tools_fallback = tools_fallback
        self.llm_mentah = llm_mentah
        self.top_k_tools = top_k_tools
        self.max_ragquery_lstcontxtcutoff = max_ragquery_lstcontxtcutoff
        self.maks_tool_dipaksa_manual = maks_tool_dipaksa_manual
        self.llm_rag = llm_rag if llm_rag is not None else llm_mentah
        self.nama_tools_hanya_tulis = frozenset(nama_tools_hanya_tulis)

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
        return {"tools_dipaksa_manual": None}

    def bangun_query(
        self, messages_raw: list, current_skill_trace: list, current_task_desc: str,
        current_task_desc_full: str, baru_saja_tutup_task: bool = False,
    ) -> str:
        """
        Wrapper publik SYNC (murni string-building,
        TANPA I/O) atas `_bangun_query_rag` -- dipisah dari `pilih_llm` lama
        supaya query-nya bisa disiapkan SEBELUM retrieval Tool-RAG dikirim
        ke thread-pool (lihat `AIBrainProcessor._jalankan_io_paralel`).
        """
        return self._bangun_query_rag(
            messages_raw, current_skill_trace, current_task_desc, current_task_desc_full,
            baru_saja_tutup_task,
        )

    def retrieve(self, query_rag: str, gorilla_aktif_override: Optional[bool] = None) -> Optional[list]:
        aktif_efektif = self.aktif_default if gorilla_aktif_override is None else gorilla_aktif_override
        if self.tool_registry is None or not aktif_efektif:
            return None
        if not query_rag:
            return list(self.tools_fallback)
        return self.tool_registry.get_relevant_tools(query_rag, 
                                                     top_k=self.top_k_tools, 
                                                     llm=self.llm_rag # LLM ringan khusus retrieval (HyDE/rerank), bukan llm_mentah
                                                     )

    def rakit_llm(
        self, *, query_rag: str, tools_relevan: Optional[list],
        skills_sukses: list, current_skill_trace: list,
        tools_dipaksa_manual: Optional[list] = None,
        mode_tulis_skill_aktif: bool = True,
    ):
        if tools_relevan is None:
            tools_final = (
                self.tools_fallback if mode_tulis_skill_aktif
                else [t for t in self.tools_fallback if t.name not in self.nama_tools_hanya_tulis]
            )
            return self.llm_mentah.bind_tools(tools_final), None

        tools_relevan = list(tools_relevan)
        if not mode_tulis_skill_aktif:
            tools_relevan = [t for t in tools_relevan if t.name not in self.nama_tools_hanya_tulis]
        rag_tool_names = {t.name for t in tools_relevan}

        # INJEKSI PAKSA TOOL DARI SKILL LIBRARY SUKSES
        if skills_sukses:
            skill_tool_names = {
                trace.get("name")
                for s in skills_sukses
                for trace in s.get("trace", [])
                if isinstance(trace, dict) and trace.get("name")
            }
            self._paksa_masuk(
                tools_relevan, rag_tool_names, skill_tool_names,
                "[🔧 Skill Injector] Memaksa masuk tool dari masa lalu",
            )

        # INJEKSI PAKSA TOOL YANG SEDANG DIPAKAI (CURRENT TRACE)
        if current_skill_trace:
            active_tool_names = {
                tc.get("name")
                for tc in current_skill_trace
                if isinstance(tc, dict) and tc.get("name")
            }
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
        mode_tulis_skill_aktif: bool = True,
    ):
        """
        Versi non-paralel (dipertahankan untuk backward-compat/testing) --
        panggil `bangun_query` -> `retrieve` -> `rakit_llm` secara berurutan.
        Jalur utama `_orchestrator` SEKARANG memparalelkan retrieval-nya
        lewat `AIBrainProcessor._jalankan_io_paralel`, tidak lewat sini.
        """
        query_rag = self.bangun_query(
            messages_raw, current_skill_trace, current_task_desc, current_task_desc_full,
            baru_saja_tutup_task,
        )
        tools_relevan = self.retrieve(query_rag, gorilla_aktif_override)
        return self.rakit_llm(
            query_rag=query_rag, tools_relevan=tools_relevan, skills_sukses=skills_sukses,
            current_skill_trace=current_skill_trace, tools_dipaksa_manual=tools_dipaksa_manual,
            mode_tulis_skill_aktif=mode_tulis_skill_aktif,
        )


def _geser_agar_tidak_memutus_pasangan_tool(semua_pesan_asli: list, titik_potong: int) -> int:
    while titik_potong > 0 and getattr(semua_pesan_asli[titik_potong], "type", None) == "tool":
        titik_potong -= 1
    return max(titik_potong, 0)


def hitung_perintah_hapus_pesan_lama(semua_pesan_asli: list, anchor_id: Optional[str], batas_simpan_db: int) -> list:
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

    titik_potong = _geser_agar_tidak_memutus_pasangan_tool(
        semua_pesan_asli, len(semua_pesan_asli) - batas_simpan_db
    )
    pesan_usang = semua_pesan_asli[:titik_potong]
    if id_pesan_dilindungi:
        pesan_usang = [m for m in pesan_usang if getattr(m, "id", None) not in id_pesan_dilindungi]

    return [RemoveMessage(id=msg.id) for msg in pesan_usang if msg.id]


def hapus_pesan_task_dibatalkan(messages_raw: list, anchor_id_lama: Optional[str]) -> list:
    """
    Menghapus dari anchor (inklusif) sampai SEBELUM pesan TERAKHIR (index -1,
    yaitu ToolMessage konfirmasi tools_batal) -- pesan konfirmasi itu SENGAJA
    dipertahankan, supaya model tetap tahu barusan ada aksi cancel, tapi
    tanpa detail task lama yang memancingnya buat lanjut.
    """
    if not anchor_id_lama or len(messages_raw) < 2:
        return []
    idx_anchor = next(
        (i for i, m in enumerate(messages_raw) if getattr(m, "id", None) == anchor_id_lama),
        None,
    )
    if idx_anchor is None:
        return []
    pesan_dihapus = messages_raw[idx_anchor:-1]
    return [RemoveMessage(id=m.id) for m in pesan_dihapus if getattr(m, "id", None)]