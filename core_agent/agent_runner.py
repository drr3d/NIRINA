import os
import json
import logging
import threading
from typing import Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import config_path
from .agent_graph import get_agent_engine
from .agent_adapter import format_state_to_response

logger = logging.getLogger(__name__)

# ==========================================
# 1. BACA KONFIGURASI DARI config.json (TIDAK BERUBAH)
# ==========================================
target_graph_file = "graph_config"
target_config_listname = "HIERARCHICAL_GRAPH_CONFIG"

if config_path.exists():
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
            target_graph_file = config_data.get("active_graph_file", target_graph_file)
            target_config_listname = config_data.get("active_graph_listname", target_config_listname)
    except Exception as e:
        logger.warning(f"⚠️ [CONFIG WARNING] Gagal membaca config.json, memakai default. Detail: {e}")

# ==========================================
# 2. INISIASI ENGINE -- SATU-SATUNYA TEMPAT INI TERJADI DI SELURUH SISTEM
# ==========================================
print(f"[⚙️ BOOTSTRAP] Menginisiasi Agent Engine dari file: {target_graph_file}.py (List: {target_config_listname})")
engine = get_agent_engine(default_env=target_graph_file, config_listname=target_config_listname)

# ==========================================
# 🚦 SEMAPHORE ANTRIAN LLM
# ==========================================
# Sekarang BENAR-BENAR efektif untuk semua channel -- karena semua channel
# (Telegram, Streamlit, integrasi API pihak ketiga) manggil lewat proses
# HTTP ini, bukan masing-masing punya engine sendiri lagi. Tetap disarankan
# JUGA set `OLLAMA_NUM_PARALLEL=1` di sisi Ollama sebagai lapisan pertahanan
# kedua (kalau-kalau ada proses lain di luar sistem ini yang ikut manggil
# Ollama yang sama).
_MAX_CONCURRENT_LLM_CALLS = int(os.environ.get("AGENT_MAX_CONCURRENT_LLM", "1"))
_llm_semaphore = threading.Semaphore(_MAX_CONCURRENT_LLM_CALLS)


def _jalankan_agent(user_input: str = None, thread_id: str = "session_001",
                     is_approval: bool = False, user_role: str = "Staff") -> Dict[str, Any]:
    """
    Logic INTI -- dipanggil oleh endpoint /chat dan /approve di bawah.
    Ini pengganti `proses_chat_agent` versi lama; namanya sengaja dibedain
    (underscore depan) supaya jelas ini fungsi INTERNAL proses API, BUKAN
    lagi API publik yang diimpor channel lain -- channel lain pakai
    agent_client.proses_chat_agent() yang manggil endpoint HTTP di bawah.
    """
    from timeit import default_timer as timer
    try:
        start = timer()
        logger.info("⚠️ [_jalankan_agent] START (thread=%s, approval=%s)", thread_id, is_approval)

        dapat_slot_langsung = _llm_semaphore.acquire(blocking=False)
        if not dapat_slot_langsung:
            logger.info(
                "⏳ [_jalankan_agent] thread=%s menunggu slot LLM (maks %d bersamaan)...",
                thread_id, _MAX_CONCURRENT_LLM_CALLS,
            )
            _llm_semaphore.acquire()

        try:
            state_terbaru = engine.run(user_input, thread_id, is_approval, user_role)
        finally:
            _llm_semaphore.release()

        logger.info("⚠️ [_jalankan_agent] SELESAI: %s detik", round(timer() - start, 2))
        return format_state_to_response(state_terbaru)
    except Exception as e:
        logger.exception("⚠️ [_jalankan_agent] GAGAL: %s", e)
        return {"status": "error", "pesan": str(e)}

# ==========================================
# 3. FASTAPI APP -- INI YANG DIJALANKAN UVICORN
# ==========================================
app = FastAPI(title="Agent API (pintu masuk utama)", version="1.0")

class ChatRequest(BaseModel):
    user_input: str
    thread_id: str
    user_role: str = "Staff"

class ApprovalRequest(BaseModel):
    thread_id: str
    setuju: bool
    user_role: str = "Staff"

@app.get("/health")
def health():
    """Dipakai app.py buat nunggu server ini siap sebelum spawn Streamlit/Telegram."""
    return {"status": "ok"}

@app.post("/chat")
def chat(req: ChatRequest):
    hasil = _jalankan_agent(user_input=req.user_input, thread_id=req.thread_id, user_role=req.user_role)
    if hasil.get("download_info"):
        nama_file = hasil["download_info"]["nama_file"]
        hasil["download_info"]["download_url"] = f"/download/{nama_file}?thread_id={req.thread_id}"
    return hasil

@app.post("/approve")
def approve(req: ApprovalRequest):
    if req.setuju:
        hasil = _jalankan_agent(is_approval=True, thread_id=req.thread_id, user_role=req.user_role)
    else:
        hasil = _jalankan_agent(
            is_approval=False,
            user_input=(
                "[SYSTEM] User membatalkan aksi tool tadi. "
                "Jangan ulangi tool yang sama - tanyakan instruksi "
                "lanjutan ke user, atau hentikan proses ini kalau "
                "memang sudah tidak relevan."
            ),
            thread_id=req.thread_id,
            user_role=req.user_role,
        )
    if hasil.get("download_info"):
        nama_file = hasil["download_info"]["nama_file"]
        hasil["download_info"]["download_url"] = f"/download/{nama_file}?thread_id={req.thread_id}"
    return hasil

@app.get("/download/{nama_file}")
def download(nama_file: str, thread_id: str):
    hasil = _jalankan_agent(is_approval=False, user_input=None, thread_id=thread_id)
    download_info = hasil.get("download_info")
    if not download_info or download_info.get("nama_file") != nama_file:
        raise HTTPException(status_code=404, detail="File tidak ditemukan atau sesi sudah tidak valid.")
    path = download_info["path"]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File tidak ditemukan di server.")
    return FileResponse(path, filename=nama_file)