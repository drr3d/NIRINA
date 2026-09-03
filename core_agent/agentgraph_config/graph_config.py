from langgraph.graph import START, END
from langchain_core.messages import AIMessage
from ..agent_factory.factory_jobscrape import (
    panggil_otak_llm,
    eksekutor_safe,
    eksekutor_sensitive,
    sensitive_tools, safe_tools
)

from ..agent_router import DecisionRouter
from ..agent_nodes import AgentState

# ==========================================
# 1. PERSIAPAN ROUTER DINAMIS
# ==========================================

# Injeksi peta alat ke Router agar ia tahu rute mana yang harus dipilih
dynamic_router = DecisionRouter(
    tools_by_category={
        "safe": safe_tools,
        "sensitive": sensitive_tools,
       
    },
)
# ==========================================
# --- 3. NODE FALLBACK: GAGAL_KOSONG ---
# ==========================================
def node_gagal_kosong(state: AgentState) -> dict:
    """
    Dipanggil DecisionRouter cuma kalau respons AI tetap kosong
    setelah retry maksimal habis (lihat MAX_RETRY_KOSONG di agent_router.py).

    Sesuai PRINSIP INTI di system prompt (KEJUJURAN TOOL / ANTI-BLANK): user TIDAK
    BOLEH pernah menerima balasan kosong, walau penyebabnya kegagalan teknis di
    sisi model/Ollama, bukan kesalahan alur. Node ini cuma menyisipkan satu pesan
    jujur ke user lalu mengembalikan revision_count ke 0 untuk giliran berikutnya.
    """
    revision_count = state.get("revision_count", 0)
    print(f"\n[❌ FIX RETRY KOSONG] Retry maksimal habis (revision_count={revision_count}). Kirim fallback jujur ke user.")

    pesan_jujur = AIMessage(
        content=(
            "Maaf, saya mengalami kendala teknis saat menyusun jawaban untuk permintaan ini "
            "(percobaan berulang kali menghasilkan respons kosong). Bisa tolong ulangi lagi "
            "pertanyaan atau instruksinya?"
        )
    )
    return {
        "messages": [pesan_jujur],
        "revision_count": -revision_count,  # reset ke 0 biar tidak terbawa ke giliran berikutnya
    }

# ==========================================
# --- 3b.  NODE FALLBACK: GAGAL_LOOPING ---
# ==========================================
def node_gagal_looping(state: AgentState) -> dict:
    """
     Dipanggil DecisionRouter cuma kalau AI terdeteksi memanggil tool
    (nama+args) yang PERSIS SAMA berturut-turut melebihi MAX_TOOL_REPEAT kali
    (lihat agent_router.py). Ini pengaman supaya model yang kurang disiplin
    (mis. gara-gara kuantisasi/model tertentu) tidak menghabiskan waktu/GPU
    mengulang aksi yang sama sampai LangGraph recursion_limit tercapai lalu
    crash. Sama seperti node_gagal_kosong: kirim SATU pesan jujur ke user, lalu
    reset semua counter terkait supaya bersih untuk giliran berikutnya.
    """
    tool_repeat_count = state.get("tool_repeat_count", 0)
    revision_count = state.get("revision_count", 0)
    last_tool_names = state.get("last_tool_names", "")
    print(
        f"\n[🔁 GAGAL LOOPING] AI mengulang tool [{last_tool_names}] {tool_repeat_count}x "
        f"berturut-turut tanpa kemajuan. Kirim fallback jujur ke user."
    )

    pesan_jujur = AIMessage(
        content=(
            "Maaf, saya mendeteksi diri saya mengulang aksi yang sama beberapa kali "
            "tanpa membuat kemajuan pada permintaan ini. Saya hentikan dulu supaya "
            "tidak buang waktu -- bisa tolong beri instruksi tambahan atau perjelas "
            "apa yang perlu saya lakukan selanjutnya?"
        )
    )
    return {
        "messages": [pesan_jujur],
        "tool_repeat_count": -tool_repeat_count,  # reset ke 0
        "last_tool_signature": "",
        "last_tool_names": "",
        "revision_count": -revision_count,  # sekalian bersihkan, biar giliran berikutnya fresh
    }

# ==========================================
# 2. SKEMA GRAPH
# ==========================================
# New mechanic for Hierarchical Multi-Agent (Delegation via Tool).
HIERARCHICAL_GRAPH_CONFIG = [
    # --- A. Pendaftaran Node ---
    {"type": "node", "name": "node_ai", "func": panggil_otak_llm},
    {"type": "node", "name": "node_safe", "func": eksekutor_safe},
    {"type": "node", "name": "node_sensitive", "func": eksekutor_sensitive, "interrupt_before": True},
    # ^ interrupt_before=True disamakan dengan node_sensitive (butuh approval HITL
    # sebelum tool pentest benar2 jalan). Hapus baris ini kalau kamu mau tool
    # pentest jalan otonom tanpa approval manusia.
    {"type": "node", "name": "node_gagal_kosong", "func": node_gagal_kosong},
    {"type": "node", "name": "node_gagal_looping", "func": node_gagal_looping},

    # --- B. Pendaftaran Edge Langsung ---
    {"type": "edge", "start": START, "end": "node_ai"},
    {"type": "edge", "start": "node_safe", "end": "node_ai"},
    {"type": "edge", "start": "node_sensitive", "end": "node_ai"},
    {"type": "edge", "start": "node_gagal_kosong", "end": END},
    {"type": "edge", "start": "node_gagal_looping", "end": END},

    # --- C. Pendaftaran Conditional Edge ---
    {
        "type": "conditional_edge",
        "source": "node_ai",
        "router": dynamic_router,
        "path_map": {
            "safe": "node_safe",
            "sensitive": "node_sensitive",
            "retry_kosong": "node_ai",
            "gagal_kosong": "node_gagal_kosong",
            "gagal_looping": "node_gagal_looping", 
            "selesai": END
        }
    }
]