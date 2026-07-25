from typing import List, Any
from .agent_nodes import AgentState, sensitive_tools

# ==========================================
# --- 3. DEFINISI ROUTER (PENGATUR JALUR) ---
# ==========================================
def router_keputusan(state: AgentState) -> str:
    """
    Router AI Utama: Sangat sederhana dan anti-error.
    Jika AI butuh tool, arahkan ke tool. Jika tidak, langsung selesai (ke User).
    """
    pesan_terakhir = state["messages"][-1]
    
    # Cek apakah AI Utama memanggil fungsi/tools
    if pesan_terakhir.tool_calls:
        nama_tool = pesan_terakhir.tool_calls[0]['name']
        
        # Pengecekan tool sensitif (seperti kirim email/pesan)
        if any(nama_tool == t.name for t in sensitive_tools):
            print(f"[Log Sistem] AI memutuskan memakai Tool SENSITIF -> {nama_tool}")
            return "lanjut_ke_sensitive"
        else:
            print(f"[Log Sistem] AI memutuskan memakai Tool AMAN -> {nama_tool}")
            return "lanjut_ke_safe"
            
    # Jika tidak ada pemanggilan tool, berarti AI sudah selesai menyusun jawaban final
    print("[Log Sistem] Draf selesai. Langsung kirim jawaban ke User!")
    return "langsung_selesai"

class DecisionRouter:
    """
    Router Kelas untuk graf AI. Menggunakan prinsip modularitas agar
    mekanisme utama dapat dengan mudah dimodifikasi atau diperluas.
    """
    def __init__(self, sensitive_tools: List[Any], logger=None):
        self._sensitive_tools = sensitive_tools
        self._logger = logger or print

    def _is_sensitive(self, tool_name: str) -> bool:
        """Metode khusus untuk mengecek sensitivitas tool."""
        return any(tool_name == getattr(t, 'name', t) for t in self._sensitive_tools)

    def _tentukan_rute_tool(self, pesan_terakhir) -> str:
        """
        MEKANISME UTAMA: Di sinilah otak penentuan cabang berada.
        Jika nanti ada percabangan baru (misal: otorisasi), ubah di sini.
        """
        tool_calls = []
        
        if hasattr(pesan_terakhir, "tool_calls"):
            tool_calls = pesan_terakhir.tool_calls
        elif isinstance(pesan_terakhir, dict):
            tool_calls = pesan_terakhir.get("tool_calls", [])
            
        if tool_calls:
            is_sensitive = False
            daftar_tool_terpanggil = []
            
            # [BARU] Loop semua tool yang dipanggil AI (Proteksi Parallel Calling)
            for tc in tool_calls:
                nama_tool = tc.get('name') if isinstance(tc, dict) else getattr(tc, 'name', '')
                daftar_tool_terpanggil.append(nama_tool)
                
                if self._is_sensitive(nama_tool):
                    is_sensitive = True
            
            # [BARU] Evaluasi akhir routing
            nama_tools_log = ", ".join(daftar_tool_terpanggil)
            if is_sensitive:
                self._logger(f"[Log Router] AI memutuskan memakai Tool SENSITIF -> [{nama_tools_log}]")
                return "lanjut_ke_sensitive"
            else:
                self._logger(f"[Log Router] AI memutuskan memakai Tool AMAN -> [{nama_tools_log}]")
                return "lanjut_ke_safe"
                
        self._logger("[Log Router] Draf selesai. Langsung kirim jawaban ke User!")
        return "langsung_selesai"

    def __call__(self, state: AgentState) -> str:
        """
        PINTU MASUK (Entry Point): LangGraph hanya memanggil ini.
        Tugasnya hanya mengekstrak state dan mendelegasikan tugas ke mekanisme utama.
        """
        if not state.get("messages"):
            return "langsung_selesai"

        pesan_terakhir = state["messages"][-1]
        
        # Mendelegasikan logika ke fungsi terpisah sesuai ide Anda
        return self._tentukan_rute_tool(pesan_terakhir)