from typing import List, Any, Dict
from .agent_nodes import AgentState
from .agent_factory import sensitive_tools

# ==========================================
# --- 3. DEFINISI ROUTER (PENGATUR JALUR) ---
# ==========================================
class DecisionRouter:
    """
    Router Dinamis untuk Framework LangGraph.
    Mendukung Parallel Tool Calling dan otomatis mengamankan eksekusi 
    jika terdapat tool sensitif di antara pemanggilan multi-tool.
    """
    def __init__(self, tools_by_category: Dict[str, List[Any]], fallback_route: str = "safe", logger=None):
        self._logger = logger or print
        self.fallback_route = fallback_route
        
        # Membangun Peta Terbalik (Lookup Table) secara dinamis
        self.tool_to_category = {}
        for category, tools in tools_by_category.items():
            for t in tools:
                nama_tool = getattr(t, 'name', t)
                self.tool_to_category[nama_tool] = category

    def _tentukan_rute_tool(self, pesan_terakhir) -> str:
        """
        MEKANISME UTAMA: Dinamis mencari kategori dari semua tool yang dipanggil.
        """
        tool_calls = []
        
        if hasattr(pesan_terakhir, "tool_calls"):
            tool_calls = pesan_terakhir.tool_calls
        elif isinstance(pesan_terakhir, dict):
            tool_calls = pesan_terakhir.get("tool_calls", [])
            
        if tool_calls:
            kategori_ditemukan = set()
            daftar_tool_terpanggil = []
            
            # 1. Pindai SEMUA tool yang dipanggil secara bersamaan (Parallel Tools)
            for tc in tool_calls:
                nama_tool = tc.get('name') if isinstance(tc, dict) else getattr(tc, 'name', '')
                daftar_tool_terpanggil.append(nama_tool)
                
                # Masukkan ke set kategori
                kategori = self.tool_to_category.get(nama_tool, self.fallback_route)
                kategori_ditemukan.add(kategori)
                
            nama_tools_log = ", ".join(daftar_tool_terpanggil)
            
            # 2. SISTEM PRIORITAS: 
            # Jika ada kategori selain rute default (misal: ada 'sensitive'), WAJIB belok ke sana
            kategori_tujuan = self.fallback_route
            
            # Ambil semua kategori yang BUKAN fallback
            kategori_khusus = [k for k in kategori_ditemukan if k != self.fallback_route]
            
            if kategori_khusus:
                # Jika ada 'sensitive', utamakan. Jika kategori custom lain, ambil yang pertama
                if "sensitive" in kategori_khusus:
                    kategori_tujuan = "sensitive"
                else:
                    kategori_tujuan = kategori_khusus[0]

            self._logger(f"[Log Router] AI memakai Tool [{nama_tools_log}] -> Rute ke Kategori: '{kategori_tujuan}'")
            return kategori_tujuan
            
        self._logger("[Log Router] Draf selesai. Langsung kirim jawaban ke User!")
        return "selesai"

    def __call__(self, state: AgentState) -> str:
        """
        PINTU MASUK (Entry Point): LangGraph hanya memanggil ini.
        """
        if not state.get("messages"):
            return "selesai"

        pesan_terakhir = state["messages"][-1]
        return self._tentukan_rute_tool(pesan_terakhir)