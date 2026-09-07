"""
Satu-satunya entrypoint proses: `python app.py`.

STRUKTUR BARU (lebih simpel dari versi sebelumnya): agent_runner.py sekarang
ADALAH API server itu sendiri -- satu-satunya proses yang pernah pegang
`engine`. Streamlit dan Telegram jadi HTTP client biasa lewat agent_client.py,
jadi app.py TIDAK PERLU LAGI menggabungkan mereka lewat asyncio.gather --
masing-masing tinggal disubprocess-kan independen, mereka gak saling
butuh berbagi memori Python sama sekali.

Alur startup:
  1. Jalankan API server (uvicorn serving agent_runner:app) di background
     thread proses ini.
  2. Tunggu endpoint /health merespons (health-check polling) -- supaya
     Streamlit/Telegram gak keburu nembak sebelum server siap.
  3. Spawn subprocess Streamlit dan/atau Telegram sesuai "main_runner" di
     config.json.
  4. Tetap hidup sampai Ctrl+C, lalu matikan semua subprocess dengan rapi.

Contoh config.json:
{
    "active_graph_file": "...",
    "active_graph_listname": "...",
    "main_runner": ["telegram", "api"]
}
"main_runner" boleh string tunggal atau list kombinasi apa saja dari
"streamlit" | "telegram" | "api". API server SELALU jalan di background
apapun isinya (Streamlit & Telegram butuh dia sebagai backend) -- entri
"api" di situ cuma menentukan host bind-nya: kalau "api" termasuk yang
diminta, server bind ke 0.0.0.0 (bisa diakses dari luar); kalau enggak,
server tetap jalan tapi cuma bind ke 127.0.0.1 (localhost-only, buat
Streamlit/Telegram doang, gak diekspos ke luar).
"""
import os
import sys
import json
import time
import threading
import subprocess
import logging
from pathlib import Path

import requests

from core_agent.config import config_path

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# default config.json look like this:
# "main_runner": ["streamlit"] -> if you need multiple runner ran at the same time just add another runner, 
#     ex: ["streamlit", "telegram"]
VALID_RUNNERS = {"streamlit", "telegram", "api"}
API_PORT = int(os.environ.get("AGENT_API_PORT", "8000"))

def _baca_main_runner() -> list:
    default = ["streamlit"]
    if not config_path.exists():
        logger.warning("⚠️ config.json tidak ditemukan, pakai default: %s", default)
        return default
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("⚠️ Gagal baca config.json (%s), pakai default: %s", e, default)
        return default

    nilai = data.get("main_runner", default)
    if isinstance(nilai, str):
        nilai = [nilai]
    elif not isinstance(nilai, list) or not nilai:
        logger.warning("⚠️ Field 'main_runner' kosong/tidak valid, pakai default: %s", default)
        return default

    tidak_dikenal = [r for r in nilai if r not in VALID_RUNNERS]
    if tidak_dikenal:
        raise ValueError(
            f"main_runner berisi nilai tidak dikenal: {tidak_dikenal}. "
            f"Pilihan valid: {sorted(VALID_RUNNERS)}"
        )
    return nilai


def _jalankan_api_server_background(host: str) -> threading.Thread:
    """
    Thread ini juga di-wrap try/except sekarang -- kalau bootstrap engine
    gagal (mis. network ke HuggingFace/Ollama bermasalah, config graph
    salah, dst), error ASLINYA ditangkap dan di-print jelas di sini, bukan
    cuma keliatan sebagai "timeout /health" yang gak jelas sumbernya.
    """
    def _run():
        try:
            import uvicorn
            uvicorn.run("core_agent.agent_runner:app", host=host, port=API_PORT, log_level="info")
        except Exception:
            logger.exception("❌ [app.py] API server GAGAL start -- lihat traceback di atas untuk akar masalahnya.")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def _tunggu_api_siap(host_untuk_healthcheck: str, api_thread: threading.Thread, timeout: float = 360.0) -> bool:
    """
    Timeout dinaikkan ke 60s (dari 30s) -- percobaan pertama load embedding
    model dari HuggingFace (kalau ada tool RAG/Knowledge Base di graph kamu)
    bisa makan waktu lebih lama dari 30s, apalagi kalau jaringan lagi lambat.
    Juga cek `api_thread.is_alive()` -- kalau thread-nya udah mati (crash),
    langsung berhenti nunggu tanpa buang waktu sampai timeout habis.

    Jika sistem anda membutuhkan waktu process yang panjang silahkan tingkatkan timeout
    """
    url = f"http://{host_untuk_healthcheck}:{API_PORT}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not api_thread.is_alive():
            logger.error("❌ [app.py] Thread API server sudah berhenti (crash) -- lihat traceback di atas.")
            return False
        try:
            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                logger.info("✅ [app.py] API server siap di %s", url)
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.5)
    logger.error("❌ [app.py] API server tidak merespons /health dalam %ss", timeout)
    return False


def _jalankan_streamlit_subprocess() -> subprocess.Popen:
    logger.info("🚀 [app.py] Meluncurkan Streamlit sebagai subprocess...")
    script = str(Path(__file__).parent / "streamlit_runner.py")
    return subprocess.Popen([sys.executable, "-m", "streamlit", "run", script])


def _jalankan_telegram_subprocess() -> subprocess.Popen:
    logger.info("🚀 [app.py] Meluncurkan Telegram bot sebagai subprocess...")
    script = str(Path(__file__).parent / "telegram_runner.py")
    return subprocess.Popen([sys.executable, script])


def main():
    runners = _baca_main_runner()
    logger.info("⚙️ [app.py] main_runner aktif: %s", runners)

    # API server SELALU jalan -- Streamlit & Telegram butuh ini sebagai
    # backend sekarang, bukan lagi opsional di antara 3 channel setara.
    expose_publik = "api" in runners
    host = "0.0.0.0" if expose_publik else "127.0.0.1"
    host_untuk_healthcheck = "127.0.0.1"  # selalu bisa diakses dari proses ini sendiri

    api_thread = _jalankan_api_server_background(host)

    if not _tunggu_api_siap(host_untuk_healthcheck, api_thread):
        logger.error("❌ [app.py] Berhenti -- API server gagal start, subprocess lain tidak akan dijalankan.")
        sys.exit(1)

    subprocesses = []
    if "streamlit" in runners:
        subprocesses.append(_jalankan_streamlit_subprocess())
    if "telegram" in runners:
        subprocesses.append(_jalankan_telegram_subprocess())

    if expose_publik:
        logger.info("🌐 [app.py] API server juga di-expose publik di :%d", API_PORT)

    try:
        # Proses utama tetap hidup selama subprocess ada, atau selamanya
        # kalau cuma "api" doang yang diminta (server jalan di thread daemon).
        if subprocesses:
            for p in subprocesses:
                p.wait()
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("🛑 [app.py] Dihentikan oleh user (Ctrl+C).")
    finally:
        for p in subprocesses:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    main()