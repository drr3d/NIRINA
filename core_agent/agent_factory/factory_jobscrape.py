from langgraph.prebuilt import ToolNode

from ..agent_cerebral import AIBrainProcessor
from ..agent_router import RouterConfig
from ..systemprompt import system_prompt
from ..registry import ToolRegistry

from .factory_skilllib import defaults_kill_lib
from .agent_factory import (
    buat_llm, factory_tools_init,
    top_k_tools_agent, maks_umur_skill_gagal_detik,
    min_similarity_skill_sukses, min_similarity_skill_gagal,
    aktifkan_gorilla_tool_rag, DynamicTokenRouterLLM,
)

# Inisialisasi konfigurasi untuk mengambil properti enable_parallel
router_config = RouterConfig()

_llms_ollama = buat_llm(
    "main", 
    model_default="qwen3.5:4b",
    provider_default="ollama", 
    num_ctx_default=32768, 
    reasoning_default=True,
)

_llms_groq = buat_llm(
    "main", 
    model_default="qwen/qwen3.6-27b", 
    provider_default="groq",
    #max_tokens=2048,  # Batas aman, cukup untuk 4-5 paragraf rencana teknis
    max_retries=3
)

LLMs = DynamicTokenRouterLLM(
    llm_chain=[
        {"llm": _llms_groq,   "nama": "Groq (cloud)",  "threshold": 8000},
        {"llm": _llms_ollama, "nama": "Ollama (lokal)", "threshold": None},
    ],
)

skill_lib = defaults_kill_lib

factory_tools_init(
    "lihat_katalog_tools",
)

# ==========================================
# Kumpulkan tools 
print(
    f"\n[⚙️ Config] Gorilla Tool-RAG: {'AKTIF' if aktifkan_gorilla_tool_rag else 'NONAKTIF (fallback ke bind semua tool statis)'}"
    + (f" | top_k_tools={top_k_tools_agent}" if aktifkan_gorilla_tool_rag else "")
)
panggil_otak_llm = AIBrainProcessor(LLMs, ToolRegistry.get_all_tools(), 
                                    system_prompt, enable_optimization=True,
                                    skill_library=skill_lib,
 
                                    maks_umur_skill_gagal_detik=maks_umur_skill_gagal_detik,
                                    min_similarity_skill_sukses=min_similarity_skill_sukses,
                                    min_similarity_skill_gagal=min_similarity_skill_gagal,

                                    tool_registry=(ToolRegistry if aktifkan_gorilla_tool_rag else None),
                                    top_k_tools=top_k_tools_agent)

# 2. Tarik alat dari kategori default (hasil dari cara lama is_sensitive)
safe_tools = ToolRegistry.get_tools("safe")
sensitive_tools = ToolRegistry.get_tools("sensitive")

# 3. Sediakan node standar agar DEFAULT_GRAPH_CONFIG tidak error
eksekutor_safe = ToolNode(safe_tools)
eksekutor_sensitive = ToolNode(sensitive_tools)