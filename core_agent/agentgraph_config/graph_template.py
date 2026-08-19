from langgraph.graph import START, END
from ..agent_factory.factory_template import (
    AI, eksekutor_safe, safe_tools
)
from ..agent_router import DecisionRouter

# ==========================================
# 1. PERSIAPAN ROUTER MINIMAL
# ==========================================
dynamic_router = DecisionRouter(
    tools_by_category={
        "safe": safe_tools  # Cukup 1 kategori alat untuk template dasar
    }
)

# ==========================================
# 2. SKEMA GRAPH
# ==========================================
HIERARCHICAL_GRAPH_CONFIG = [
    # --- A. Pendaftaran Node ---
    {"type": "node", "name": "node_ai", "func": AI},
    {"type": "node", "name": "node_safe", "func": eksekutor_safe},

    # --- B. Pendaftaran Edge Langsung ---
    {"type": "edge", "start": START, "end": "node_ai"},
    {"type": "edge", "start": "node_safe", "end": "node_ai"},

    # --- C. Pendaftaran Conditional Edge (Penentu Arah Eksekusi) ---
    # Untuk arsitektur AI Agent (ReAct Pattern), conditional_edge WAJIB 100%.
    # Tanpa conditional_edge, agen tidak punya cara untuk mengambil keputusan secara dinamis di tengah eksekusi.
    {
        "type": "conditional_edge",
        "source": "node_ai",
        "router": dynamic_router,
        "path_map": {
            "safe": "node_safe",  # Jika AI panggil tool -> Lanjut ke eksekutor
            "selesai": END         # Jika AI selesai menjawab -> Keluar Graph
        }
    }
]