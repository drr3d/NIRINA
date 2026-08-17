import json
import logging
from .config import config_path
from .agent_graph import get_agent_engine
from .agent_adapter import StreamlitAgentAdapter

logger = logging.getLogger(__name__)

# ==========================================
# 1. BACA KONFIGURASI DARI config.json
# ==========================================
# Nilai default jika config.json kosong atau rusak
target_graph_file = "graph_config"
target_config_listname = "HIERARCHICAL_GRAPH_CONFIG"

if config_path.exists():
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
            # Ambil konfigurasi nama file dan nama list variabel
            target_graph_file = config_data.get("active_graph_file", target_graph_file)
            target_config_listname = config_data.get("active_graph_listname", target_config_listname)
    except Exception as e:
        logger.warning(f"⚠️ [CONFIG WARNING] Gagal membaca config.json, memakai default. Detail: {e}")

# ==========================================
# 2. INISIASI ENGINE (Aman dengan Streamlit Cache)
# ==========================================
print(f"[⚙️ BOOTSTRAP] Menginisiasi Agent Engine dari file: {target_graph_file}.py (List: {target_config_listname})")
engine = get_agent_engine(default_env=target_graph_file, config_listname=target_config_listname)

# ==========================================
# 3. FUNGSI UTAMA UNTUK STREAMLIT UI
# ==========================================
def proses_chat_agent(user_input: str = None, thread_id: str = "session_001",
                       is_approval: bool = False, user_role: str = "Staff") -> dict:
    from timeit import default_timer as timer
    try:
        start = timer()
        logger.info("⚠️ [proses_chat_agent] START")
        
        # Eksekusi engine yang sudah dirakit
        state_terbaru = engine.run(user_input, thread_id, is_approval, user_role)
        
        logger.info("⚠️ [proses_chat_agent] SELESAI: %s detik", round(timer() - start, 2))
 
        return StreamlitAgentAdapter.process_state_to_ui(state_terbaru)
    except Exception as e:
        logger.exception("⚠️ [proses_chat_agent] GAGAL: %s", e)
        return {"status": "error", "pesan": str(e)}