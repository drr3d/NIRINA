from langchain_core.messages import HumanMessage
from ..agent_nodes import AgentState
from ..registry import FailsafeRegistry

# ==========================================
# 1. NODE FACTORIES (Pola Nudge & Fallback)
# ==========================================
def _node_nudge(marker: str, teks: str):
    """Factory: node yang nempelin 1 pesan nudge (dipersist ke messages)."""
    def _node(state: AgentState) -> dict:
        return {"messages": [HumanMessage(content=f"{marker} {teks}")]}
    return _node

def _node_gagal(kode: str, default_pesan: str):
    """Factory: node fallback jujur lewat FailsafeRegistry untuk 1 kode kegagalan."""
    def _node(state: AgentState) -> dict:
        return FailsafeRegistry.get_update(kode, state, default_pesan)
    return _node

# Instansiasi Node Generik untuk Subgraph
_node_paksa_retry = _node_nudge("[SISTEM]",
    "Balasanmu barusan HANYA berisi rencana tanpa tool call. JANGAN ulangi itu -- "
    "panggil tool yang relevan SEKARANG, di balasan ini juga.")

_node_cegah_loop = _node_nudge("[SISTEM-LOOP]",
    "Tool call barusan SAMA PERSIS dengan yang sudah pernah kamu jalankan di giliran "
    "ini -- JANGAN diulang. Lanjutkan ke langkah berikutnya, atau berikan laporan "
    "akhir SEKARANG tanpa tool call.")

_node_gagal_tanpa_tool = _node_gagal("tanpa_tool",
    "Maaf, saya kesulitan menyelesaikan pemeriksaan ini secara otomatis setelah "
    "beberapa kali percobaan. Coba ulangi permintaannya, atau persempit fokus pertanyaannya.")

_node_gagal_loop = _node_gagal("loop_terdeteksi",
    "Maaf, saya terjebak mengulang langkah yang sama saat memproses permintaan ini. "
    "Coba ulangi lagi permintaannya, atau persempit fokus pertanyaannya.")