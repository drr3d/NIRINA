import operator
import json
from typing import Annotated, TypedDict, Any
import importlib
import pkgutil

# Import LangChain & LangGraph components
from langchain_core.messages import SystemMessage
from langchain_ollama import ChatOllama
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

# Import dari file yang lain
from .systemprompt_collection import system_prompt
from .config import config_path, app_dir
from .registry import ToolRegistry

def optimasi_konteks_langchain(messages):
    """
    Optimasi berbasis 'Sliding Window':
    Menjaga data tool tetap utuh untuk pertanyaan saat ini, 
    namun mengompres data tool dari masa lalu untuk menghemat VRAM.
    """
    from langchain_core.messages import ToolMessage
    
    cleaned_messages = []
    total_msgs = len(messages)
    
    # Batas Jendela Memori: Pertahankan isi penuh dari 8 pesan terakhir (sekitar 2-3 turn tanya jawab)
    BATAS_PESAN_AKTIF = 8 
    
    for idx, msg in enumerate(messages):
        # 1. System Prompt WAJIB aman
        if msg.type == "system":
            cleaned_messages.append(msg)
            continue
            
        # 2. Cek apakah ini pesan dari obrolan yg lalu
        is_pesan_lama = (total_msgs - idx) > BATAS_PESAN_AKTIF
        
        if msg.type == "tool":
            # Jika pesan tool sudah usang (di luar batas aktif) DAN isinya panjang, baru dikompres!
            if is_pesan_lama and len(msg.content) > 300:
                # Membuat obyek baru yang aman (bukan .copy() yang berbahaya)
                pesan_kompresi = ToolMessage(
                    content=f"[Log memori: Data teknis '{msg.name}' dikompresi karena percakapan telah berlalu. Jika butuh, panggil ulang tool-nya.]",
                    name=msg.name,
                    tool_call_id=msg.tool_call_id
                )
                cleaned_messages.append(pesan_kompresi)
            else:
                # Tool di percakapan SAAT INI (baik JSON maupun RAG) akan lolos 100% utuh!
                cleaned_messages.append(msg)
        else:
            # Pesan Human dan AI dibiarkan utuh agar obrolan tidak amnesia
            cleaned_messages.append(msg)
            
    return cleaned_messages

# ==========================================
# --- 1. ARSITEKTUR CUSTOM STATEGRAPH ---
# ==========================================
class AgentState(TypedDict):
    """
    Representasi memori sentral untuk AI Agent.
    - messages: Menyimpan riwayat obrolan (ditumpuk).
    - revision_count: Menghitung berapa kali AI sudah direvisi.
    """
    messages: Annotated[list, add_messages]
    revision_count: Annotated[int, operator.add]
    pending_tasks: str # <-- Tambahan baru, untuk monitoring pending task

# ==========================================
# --- 2. DEFINISI NODE (KOMPONEN AI) ---
# ==========================================
class AIBrainProcessor:
    """
    Komponen Otak Utama (Brain Node) untuk AI Agent.
    """
    
    def __init__(self, llm_model: Any, tools_list: list, base_prompt: str):
        # Binding tools cukup dilakukan sekali saat class dibentuk
        # (Memakai variabel 'llm' dan 'tools' global yang sudah di-import di file ini)
        self.base_prompt = base_prompt
        self._llm_with_tools = llm_model.bind_tools(tools_list)

    def _build_system_prompt(self, pending_tasks: str) -> str:
        """Menggabungkan prompt dasar dengan status tugas yang masih gantung."""
        # Memakai variabel 'system_prompt' global dari collection
        prompt = self.base_prompt 
        if pending_tasks:
            prompt += (
                f"\n\n[🚨 PERINGATAN SISTEM: Kamu memiliki instruksi dari user yang masih tertunda:\n"
                f"{pending_tasks}\n"
                f"Segera tindak lanjuti jika user sudah memberikan data yang dibutuhkan!]"
            )
        return prompt

    def _extract_pending_tasks(self, response_content: str) -> str:
        """Mengekstrak blok To-Do list (Scratchpad) dari balasan AI."""
        if not response_content:
            return ""
            
        marker = "### 📝 Status Tugas Aktif"
        if marker in response_content:
            parts = response_content.split(marker)
            if len(parts) > 1:
                return parts[1].strip()
        return ""

    def _orchestrator(self, state: AgentState):
        """
        Entry point yang dieksekusi oleh LangGraph.
        Strukturnya dipertahankan sesuai fungsi panggil_otak_llm aslinya.
        """
        # Gunakan list() agar tidak mengubah pointer asli
        messages = list(state.get("messages", []))
        pending_tasks = state.get("pending_tasks", "")

        # 1. Siapkan & Injeksi System Prompt Dinamis
        current_system_prompt = self._build_system_prompt(pending_tasks)
        
        if messages and isinstance(messages[0], SystemMessage):
            messages[0] = SystemMessage(content=current_system_prompt)
        else:
            messages.insert(0, SystemMessage(content=current_system_prompt))

        # 2. Pangkas konteks usang (Memakai fungsi global)
        messages_dioptimalkan = optimasi_konteks_langchain(messages)
        
        print("\n[Log Sistem] AI Utama sedang menganalisis input atau menyusun jawaban...")
        
        # 3. Panggil LLM
        response = self._llm_with_tools.invoke(messages_dioptimalkan)
        
        print("\n--- [DAPUR AGENT: APA YANG DIPIKIRKAN LLM?] ---")
        print(f"Content: {response.content}") 
        print(f"Tool Calls: {response.tool_calls}") 
        print("----------------------------------------------\n")
        
        # 4. Siapkan state balasan
        update_state = {"messages": [response]}
        
        # 5. simpan status task
        if response.content:
            update_state["pending_tasks"] = self._extract_pending_tasks(response.content)
        else:
            # Jika respon hanya memanggil tool tanpa teks, biarkan task pending sebelumnya (jangan ditimpa string kosong)
            # Kecuali jika ingin meresetnya. Untuk amannya, kita abaikan update jika tidak ada text.
            pass

        return update_state

    def __call__(self, state: AgentState) -> dict:
        return self._orchestrator(state)

# --- AUTO-DISCOVERY PLUGIN ---
# Sistem akan membaca otomatis semua file python di folder 'plugins'
# Memanfaatkan app_dir yang sudah dideklarasikan di atas
plugin_folder = app_dir / "plugins"

if plugin_folder.exists():
    # pkgutil.iter_modules membutuhkan list of string path
    for _, module_name, _ in pkgutil.iter_modules([str(plugin_folder)]):
        importlib.import_module(f"plugins.{module_name}")

# Kumpulkan tools untuk diexport ke agent_nodes.py
safe_tools = ToolRegistry.safe_tools
sensitive_tools = ToolRegistry.sensitive_tools
tools = safe_tools + sensitive_tools

# --- [DYNAMIC CONFIG] MEMBACA SETTING MODEL UNTUK CHAT AGENT ---
model_chat_name = "qwen2.5:1.5b" # Default fallback jika config tidak ada
if config_path.exists():
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
            # Mengambil model_chat_agent, jika tidak diset otomatis pakai qwen2.5:1.5b
            model_chat_name = config_data.get("model_chat_agent", "qwen2.5:1.5b")
    except Exception:
        pass

# LLM sebagai "Otak" Agent dinamis berdasarkan konfigurasi.
# beberapa setting num_ctx: 4086, 8192, 12288, 16384, 20484, 32768
LLMs = ChatOllama(model=model_chat_name, temperature=0.3, num_ctx=20484)

panggil_otak_llm = AIBrainProcessor(LLMs, tools, system_prompt)

# NODE 3 & 4: Tangan Eksekutor Tool
eksekutor_safe = ToolNode(safe_tools)
eksekutor_sensitive = ToolNode(sensitive_tools)