from langgraph.graph import START, END
from ..agent_nodes import (
    panggil_otak_llm,
    eksekutor_safe,
    eksekutor_sensitive,
)

from ..agent_router import DecisionRouter
# Instansiasi router, masukkan dependencies-nya
from ..agent_nodes import sensitive_tools
dynamic_router = DecisionRouter(sensitive_tools=sensitive_tools)

# SKEMA GRAF DEFAULT
DEFAULT_GRAPH_CONFIG = [
    # 1. Pendaftaran Node
    {"type": "node", "name": "node_ai", "func": panggil_otak_llm},
    {"type": "node", "name": "node_safe", "func": eksekutor_safe},
    {
        "type": "node", 
        "name": "node_sensitive", 
        "func": eksekutor_sensitive,
        "interrupt_before": True  # <--- This Important!
    },

    # 2. Pendaftaran Edge Langsung
    {"type": "edge", "start": START, "end": "node_ai"},
    {"type": "edge", "start": "node_safe", "end": "node_ai"},
    {"type": "edge", "start": "node_sensitive", "end": "node_ai"},

    # 3. Pendaftaran Conditional Edge
    {
        "type": "conditional_edge",
        "source": "node_ai",
        "router": dynamic_router,
        "path_map": {
            "lanjut_ke_safe": "node_safe",
            "lanjut_ke_sensitive": "node_sensitive",
            "langsung_selesai": END
        }
    }
]

# Another Examples
'''
ADVANCED_CONTRIBUTOR_CONFIG = [
    # Node biasa dengan Interrupt dinamis (Minta Izin sebelum jalan)
    {
        "type": "node", 
        "name": "node_sensitive", 
        "func": eksekutor_sensitive,
        "interrupt_before": True  # <-- Dinamis! Tidak perlu hardcode di compile lagi
    },
    
    # Subgraph (Memasang Graf Terpisah milik kontributor sebagai Node)
    {
        "type": "node",
        "name": "node_sistem_pajak",
        "func": compiled_subgraph_pajak # <-- Objek Subgraph LangGraph
    },
    
    # Parallel Edge (Fan-Out: Jalankan 2 node sekaligus secara bersamaan)
    {
        "type": "edge",
        "start": START,
        "end": ["node_ai", "node_sistem_pajak"] # <-- PARALEL!
    }
]
'''