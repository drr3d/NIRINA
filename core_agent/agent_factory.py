# ==========================================
# File: core_agent/agent_components.py
# ==========================================
import importlib
import pkgutil
import json
from langgraph.prebuilt import ToolNode
from langchain_ollama import ChatOllama

# Import class inti dari agent_nodes
from .agent_nodes import AIBrainProcessor
from .systemprompt_collection import system_prompt
from .config import config_path, app_dir
from .registry import ToolRegistry

# --- AUTO-DISCOVERY PLUGIN ---
plugin_folder = app_dir / "plugins"
if plugin_folder.exists():
    for _, module_name, _ in pkgutil.iter_modules([str(plugin_folder)]):
        importlib.import_module(f"plugins.{module_name}")

# --- [DYNAMIC CONFIG] MEMBACA SETTING MODEL ---
model_chat_name = "qwen2.5:1.5b" 
if config_path.exists():
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
            model_chat_name = config_data.get("model_chat_agent", "qwen2.5:1.5b")
    except Exception:
        pass

# num_ctx = 4096, 8192, 16384
LLMs = ChatOllama(model=model_chat_name,
                  temperature=0.3, 
                  num_ctx=20484,
                  reasoning=False,   # <- matikan thinking, ini pemicu bug kosong di qwen3.5+tools
                  )

# ==========================================
# ==========================================
# Kumpulkan tools 
panggil_otak_llm = AIBrainProcessor(LLMs, ToolRegistry.get_all_tools(), system_prompt, enable_optimization=True)

# 2. Tarik alat dari kategori default (hasil dari cara lama is_sensitive)
safe_tools = ToolRegistry.get_tools("safe")
sensitive_tools = ToolRegistry.get_tools("sensitive")

# 3. Sediakan node standar agar DEFAULT_GRAPH_CONFIG tidak error
eksekutor_safe = ToolNode(safe_tools)
eksekutor_sensitive = ToolNode(sensitive_tools)