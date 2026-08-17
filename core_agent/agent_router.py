from typing import List, Any, Dict, Union
from .agent_nodes import AgentState

# ==========================================
# 🎚️ TOGGLE PARALLEL TOOL CALLING
# ==========================================
# False = (Aman untuk RAM/GPU Kecil) AI hanya dieksekusi satu per satu sesuai prioritas.
# True  = (Butuh Spek Tinggi) Mengaktifkan Fan-out LangGraph. Agen/Node akan berjalan bersamaan.
ENABLE_PARALLEL_ROUTING = False

# ============================================
# --- 3. DEFINISI ROUTER ---
# ============================================
class DecisionRouter:
    """
    Router Dinamis untuk Framework LangGraph.
    Mendukung Parallel Tool Calling dan otomatis mengamankan eksekusi 
    jika terdapat tool sensitif di antara pemanggilan multi-tool.
    """

    MAX_RETRY_KOSONG = 2   # maksimal 2x coba ulang respons kosong sebelum menyerah dengan jujur
    MAX_TOOL_REPEAT = 3    # maksimal 3x panggilan tool identik (nama+args sama) berturut-turut

    # ==========================================
    # 🕵️‍♂️ INTERCEPT MAP (Tool Shadowing)
    # Peta nama tool dummy untuk diteruskan ke sub-agent (tanpa merusak Registry)
    # ==========================================
    SPECIAL_ROUTING = {
        "delegasi_koder": "coder",
        "konsultasi_planner": "planner"
    }

    def __init__(self, tools_by_category: Dict[str, List[Any]], fallback_route: str = "safe", logger=None):
        self._logger = logger or print
        self.fallback_route = fallback_route
        
        # Membangun Lookup Table secara dinamis
        self.tool_to_category = {}

        for category, tools in tools_by_category.items():
            for t in tools:
                nama_tool = getattr(t, 'name', t)
                self.tool_to_category[nama_tool] = category

    def _tentukan_rute_tool(
        self,
        pesan_terakhir,
        revision_count: int = 0,
        tool_repeat_count: int = 0,
    ) -> Union[str, List[str]]:
        """
        MEKANISME UTAMA: Dinamis mencari kategori dari semua tool yang dipanggil.
        """
        tool_calls = getattr(pesan_terakhir, "tool_calls", [])
        invalid_calls = getattr(pesan_terakhir, "invalid_tool_calls", [])

        # ==========================================
        # 🛡️ GUARDRAIL 1: DETEKSI TOOL CACAT (TYPO JSON)
        # ==========================================
        if invalid_calls and not tool_calls:
            self._logger(f"[Log Router] ❌ AI gagal memformat Tool JSON dengan benar. Memaksa loop balik (retry_kosong)!")
            return "retry_kosong"

        daftar_tool_terpanggil = []
        if tool_calls:
            # ==========================================
            # 🔁 GUARDRAIL BARU: DETEKSI TOOL YANG DIULANG TERUS
            # ==========================================
            # Dicek SEBELUM logika kategori/prioritas -- kalau tool (nama+args)
            # yang PERSIS SAMA sudah dipanggil >= MAX_TOOL_REPEAT kali berturut-turut,
            # paksa berhenti daripada membiarkan AI terus mengulang sampai
            # recursion_limit LangGraph tercapai (lihat run() di agent_graph.py).
            if tool_repeat_count >= self.MAX_TOOL_REPEAT:
                nama_tools_log = ", ".join(
                    tc.get('name') if isinstance(tc, dict) else getattr(tc, 'name', '')
                    for tc in tool_calls
                )
                self._logger(
                    f"[Log Router] 🔁 AI mengulang tool [{nama_tools_log}] dengan argumen SAMA "
                    f"{tool_repeat_count}x berturut-turut -> paksa berhenti (gagal_looping)!"
                )
                return "gagal_looping"

            kategori_ditemukan = set()
            
            # 1. Scan SEMUA tool yang dipanggil secara bersamaan (Parallel Tools)
            for tc in tool_calls:
                nama_tool = tc.get('name') if isinstance(tc, dict) else getattr(tc, 'name', '')
                daftar_tool_terpanggil.append(nama_tool)

                # ==========================================
                # CEK INTERCEPT DI SINI 
                # ==========================================
                # Jika tool ada di map spesial, paksa kategorinya ke rute sub-agent.
                # Jika tidak, biarkan berjalan normal mengikuti Lookup Table.
                if nama_tool in self.SPECIAL_ROUTING:
                    kategori = self.SPECIAL_ROUTING[nama_tool]
                else:
                    kategori = self.tool_to_category.get(nama_tool, self.fallback_route)
                
                kategori_ditemukan.add(kategori)
                
            nama_tools_log = ", ".join(daftar_tool_terpanggil)
            
            # Ambil semua kategori yang BUKAN fallback
            kategori_khusus = [k for k in kategori_ditemukan if k != self.fallback_route]

            # ==========================================
            # 🔀 CABANG EKSEKUSI (PARALLEL VS SEQUENTIAL)
            # ==========================================
            if ENABLE_PARALLEL_ROUTING:
                # Mode Paralel: Kembalikan list semua rute yang dibutuhkan
                rute_final = list(kategori_ditemukan)
                rute_final = rute_final if len(rute_final) > 1 else rute_final[0]
                self._logger(f"[Log Router] ⚡ PARALLEL ON! AI memakai Tool [{nama_tools_log}] -> Rute Fan-out: {rute_final}")
                return rute_final
            else:
                # 2. SISTEM PRIORITAS: 
                # Jika ada kategori selain rute default (misal: ada 'sensitive' atau sub-agent), WAJIB belok ke sana
                kategori_tujuan = self.fallback_route
                
                if kategori_khusus:
                    # Intercept (coder/planner) atau sensitive harus menang mutlak sesuai prioritas
                    if "coder" in kategori_khusus:
                        kategori_tujuan = "coder"
                    elif "planner" in kategori_khusus:
                        kategori_tujuan = "planner"
                    elif "pentest" in kategori_khusus:
                        kategori_tujuan = "pentest"
                    elif "sensitive" in kategori_khusus:
                        kategori_tujuan = "sensitive"
                    else:
                        kategori_tujuan = kategori_khusus[0]

                self._logger(f"[Log Router] 🚦 PARALLEL OFF! AI memakai Tool [{nama_tools_log}] -> Rute ke Kategori: '{kategori_tujuan}'")
                return kategori_tujuan

        konten = getattr(pesan_terakhir, "content", "") or ""

        # ==========================================
        # 🛡️ GUARDRAIL 2: RESPONS KOSONG (TIDAK ADA TOOL CALLS SAMA SEKALI)
        # ==========================================
        if not konten.strip():
            print(f"[_tentukan_rute_tool] konten kosong, revision_count saat ini: {revision_count}")
            if revision_count < self.MAX_RETRY_KOSONG:
                self._logger(
                    f"[Log Router] ⚠️ Respons kosong terdeteksi (percobaan retry ke-{revision_count + 1}"
                    f"/{self.MAX_RETRY_KOSONG}) -> loop balik ke node 'otak'"
                )
                return "retry_kosong"
            self._logger(
                f"[Log Router] ❌ Respons tetap kosong setelah {self.MAX_RETRY_KOSONG}x retry "
                f"-> rute 'gagal_kosong' (kirim pesan jujur ke user)"
            )
            return "gagal_kosong"

        self._logger("[Log Router] Draf selesai. Langsung kirim jawaban ke User!")
        return "selesai"

    def __call__(self, state: AgentState) -> Union[str, List[str]]:
        """
        PINTU MASUK (Entry Point): LangGraph hanya memanggil ini.
        """
        if not state.get("messages"):
            return "selesai"

        pesan_terakhir = state["messages"][-1]
        revision_count = state.get("revision_count", 0)
        tool_repeat_count = state.get("tool_repeat_count", 0)
        return self._tentukan_rute_tool(pesan_terakhir, revision_count, tool_repeat_count)