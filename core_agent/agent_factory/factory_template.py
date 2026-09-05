from langgraph.prebuilt import ToolNode

from ..agent_cerebral import AIBrainProcessor
from .agent_factory import (
    LLMProviderRegistry, buat_llm, buat_skill_library, factory_tools_init
)
from ..registry import ToolRegistry
from ..systemprompt_collection import system_prompt # adjust with your py system prompt file

# 1. ========================================================
# ============= DEFINE/CHOSE THE BRAIN =================
# [CONTOH CUSTOM EXTENSION] USER MENAMBAH PROVIDER GEMINI
# User cukup membuat fungsi builder sesuai aturan framework, lalu me-register-nya.
def _build_google_gemini(model_name: str, temperature: float, **kwargs):
    from langchain_google_genai import ChatGoogleGenerativeAI
    
    # Hapus parameter yang tidak dikenal Gemini
    api_key = kwargs.pop("api_key", "")
    kwargs.pop("base_url", None)
    kwargs.pop("num_ctx", None)
    kwargs.pop("reasoning", None)
    
    return ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, temperature=temperature, **kwargs)

# Daftarkan ke framework!
LLMProviderRegistry.register("gemini", _build_google_gemini)

# Planner (Menggunakan Custom Provider yang baru didaftarkan di atas!)
LLM = buat_llm(
    "main", 
    model_default="gemini-2.5-flash", 
    provider_default="gemini", # <-- Memanggil custom builder Google!
)
# ==========================================================

# 1.b(Optional) [PROJECT-SPECIFIC] Skill library Voyager-style buat agent
skill_lib = buat_skill_library(
    "qa",
    persist_dir_default="./skill_library_db",
    collection_name_default="agent_skills",
    embedding_backend_default="ollama",
    ollama_model_default="nomic-embed-text",
)

# 2. Load all plugins dari dir plugins
# plugins = tools for agent
factory_tools_init() #  Read function docstring for more instruction

# 3. Define AI Orchestrator for graph
AI = AIBrainProcessor(LLM, 
                      ToolRegistry.get_all_tools(), 
                      system_prompt,
                      enable_optimization=True,
                      skill_library=skill_lib
                      )

# 4. Prepare tools
safe_tools = ToolRegistry.get_tools("safe")

# 5. Prepare Node
eksekutor_safe = ToolNode(safe_tools)