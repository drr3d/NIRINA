from langgraph.graph import START, END

from ..agent_factory import (
    panggil_otak_llm,
    eksekutor_safe,
    eksekutor_sensitive,
    sensitive_tools
)
from ..registry import ToolRegistry
from ..agent_router import DecisionRouter

# ==========================================
# 1. PERSIAPAN ROUTER DINAMIS
# ==========================================
# Kita tarik alat berdasarkan kategorinya langsung dari Registry
safe_tools = ToolRegistry.get_tools("safe")
sensitive_tools = ToolRegistry.get_tools("sensitive")

# Injeksi peta alat ke Router agar ia tahu rute mana yang harus dipilih
dynamic_router = DecisionRouter(
    tools_by_category={
        "safe": safe_tools,
        "sensitive": sensitive_tools
    }
)

# ==========================================
# 2. SKEMA GRAF DEFAULT
# ==========================================
DEFAULT_GRAPH_CONFIG = [
    # --- A. Pendaftaran Node ---
    {"type": "node", "name": "node_ai", "func": panggil_otak_llm},
    {"type": "node", "name": "node_safe", "func": eksekutor_safe},
    {
        "type": "node", 
        "name": "node_sensitive", 
        "func": eksekutor_sensitive,
        "interrupt_before": True  # Memicu HITL (Human-In-The-Loop)
    },

    # --- B. Pendaftaran Edge Langsung ---
    # Mengembalikan alur kembali ke AI setelah eksekutor selesai bekerja
    {"type": "edge", "start": START, "end": "node_ai"},
    {"type": "edge", "start": "node_safe", "end": "node_ai"},
    {"type": "edge", "start": "node_sensitive", "end": "node_ai"},

    # --- C. Pendaftaran Conditional Edge ---
    {
        "type": "conditional_edge",
        "source": "node_ai",
        "router": dynamic_router,
        "path_map": {
            # Key (kiri) adalah string hasil kembalian dari DecisionRouter
            # Value (kanan) adalah nama node tujuan
            "safe": "node_safe",
            "sensitive": "node_sensitive",
            "selesai": END
        }
    }
]