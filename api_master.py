import os
import time
import threading
import asyncio
import logging
from queue import Queue
from pathlib import Path

# --- Flask & Watchdog Imports ---
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- Telegram Imports ---
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CallbackQueryHandler

import sys
import time

from core_agent.agent_graph import proses_chat_agent # Import fungsi AI-mu

USER_LOCKS = {}

# 1. Konfigurasi Path & Import Eksternal
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

# Pastikan module textprocessor dan config sudah ada sesuai kodemu sebelumnya
try:
    from database.textprocessor import process_cv
    from core_agent.config import app_dir
    UPLOAD_DIR = (app_dir / "../RESUME").resolve()
except ImportError:
    # Fallback jika module tidak ditemukan saat testing
    UPLOAD_DIR = Path("../RESUME").resolve()
    def process_cv(filepath):
        time.sleep(3) # Simulasi AI berpikir
        return True

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 2. Konfigurasi Bot Telegram
TG_TOKEN = 'PLACE_YOUR_BOT_TOKEN_HERE'
MAX_FILE_SIZE = 2 * 1024 * 1024  # Naikkan ke 2 MB menyesuaikan standar UI
ALLOWED_EXTENSIONS = {'.pdf', '.doc', '.docx', '.txt'}

# 3. State Management (Untuk UI Dashboard)
cv_queue = Queue()
SERVICE_STATE = {
    "status": "RUNNING", 
    "processed": 0, 
    "failed": 0, 
    "current_file": "-",
    "tg_bot_status": "AKTIF 🟢"
}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================================
# AI Chat Handler, Bridge telegram to Qwen
# =========================================

# Dictionary untuk menyimpan sesi chat (Temporary Storage)
ACTIVE_CHATS = {}
SESSION_TIMEOUT = 3600  # 3600 detik = 1 jam

def update_chat_status(chat_id, username, status):
    """Fungsi untuk mencatat aktivitas user ke memori"""
    ACTIVE_CHATS[str(chat_id)] = {
        "username": username,
        "status": status,
        "last_active": time.time()
    }

def bersihkan_chat_lama():
    """Menghapus sesi yang tidak aktif lebih dari 1 jam"""
    waktu_sekarang = time.time()
    # Cari ID yang sudah kedaluwarsa
    id_kedaluwarsa = [
        cid for cid, data in ACTIVE_CHATS.items() 
        if waktu_sekarang - data["last_active"] > SESSION_TIMEOUT
    ]
    # Hapus dari memori
    for cid in id_kedaluwarsa:
        del ACTIVE_CHATS[cid]

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = str(update.message.chat_id) 
    username = update.message.from_user.first_name or "User"
    user_role = "HR Admin" 

    # ==========================================
    # 🛡️ 1. CEK STATUS LOCK (MENCEGAH SPAM)
    # ==========================================
    if USER_LOCKS.get(chat_id, False):
        # Jika statusnya True (masih proses), abaikan pesan baru dan beri peringatan
        #pesan_spam = await update.message.reply_text("⚠️ *Mohon tunggu, AI masih memproses permintaanmu sebelumnya...*", parse_mode='Markdown')
        
        # Hapus pesan peringatan setelah 3 detik agar chat tidak kotor
        #await asyncio.sleep(3)
        #await pesan_spam.delete()
        return # <-- Langsung berhenti di sini, chat spam tidak akan diantrekan.

    # KUNCI CHAT UNTUK USER INI (Pintu masuk ditutup)
    USER_LOCKS[chat_id] = True 

    # === UPDATE STATUS: Sedang Diproses ===
    update_chat_status(chat_id, username, "⏳ AI sedang memikirkan jawaban...")

    # ==========================================
    # 2. PROSES AI (Dalam blok try-finally)
    # ==========================================
    pesan_tunggu = await update.message.reply_text("⏳ *AI sedang memproses pertanyaanmu, mohon tunggu...*", parse_mode='Markdown')

    try:
        # [PERBAIKAN VITAL]: Panggil fungsi AI menggunakan thread latar belakang.
        # Ini mencegah bot membeku, sehingga if USER_LOCKS (di atas) tetap bisa merespons
        # dan menolak chat masuk saat AI sedang asyik berpikir.
        hasil = await asyncio.to_thread(
            proses_chat_agent,
            user_input=user_text, 
            thread_id=chat_id, 
            user_role=user_role
        )

        # Jika butuh persetujuan
        if hasil.get("status") == "butuh_persetujuan":
            # === UPDATE STATUS: Menunggu Approval ===
            update_chat_status(chat_id, username, "⚠️ Menunggu konfirmasi (HitL)")

            keyboard = [
                [
                    InlineKeyboardButton("✅ Setujui & Lanjutkan", callback_data='approve'),
                    InlineKeyboardButton("❌ Batalkan", callback_data='cancel')
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            pesan_warning = f"⚠️ **Sistem Membutuhkan Persetujuan**\n\n{hasil['pesan']}"
            
            await pesan_tunggu.edit_text(pesan_warning, reply_markup=reply_markup, parse_mode='Markdown')
        
        # Jika sukses normal
        else:
            # === UPDATE STATUS: Selesai / Idle ===
            update_chat_status(chat_id, username, "✅ Selesai (Idle)")

            await pesan_tunggu.edit_text(hasil["pesan"])
            
            if "download_info" in hasil and hasil["download_info"]:
                file_path = hasil["download_info"]["path"]
                if os.path.exists(file_path):
                    await context.bot.send_document(chat_id=chat_id, document=open(file_path, 'rb'))

    except Exception as e:
        update_chat_status(chat_id, username, "❌ Error saat memproses")
        await pesan_tunggu.edit_text(f"❌ Terjadi kesalahan pada saat memproses data: {str(e)}")
        
    finally:
        # ==========================================
        # 🔓 3. LEPASKAN KUNCI (WAJIB ADA DI FINALLY)
        # ==========================================
        # Entah AI-nya sukses, butuh approval, atau bahkan error crash, 
        # kuncinya akan selalu dibuka kembali.
        USER_LOCKS[chat_id] = False

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() 
    
    chat_id = str(query.message.chat_id)
    user_role = "HR Admin"

    # 🛡️ Cek Lock untuk tombol
    if USER_LOCKS.get(chat_id, False):
        return # Abaikan klik jika bot sedang jalan

    USER_LOCKS[chat_id] = True # Kunci

    try:
        if query.data == 'approve':
            await query.edit_message_text("✅ *Tindakan disetujui. AI sedang memproses...*", parse_mode='Markdown')
            
            # Bungkus proses persetujuan ke thread
            hasil = await asyncio.to_thread(
                proses_chat_agent,
                is_approval=True, 
                thread_id=chat_id, 
                user_role=user_role
            )
            
            while hasil.get("status") == "butuh_persetujuan":
                hasil = await asyncio.to_thread(
                    proses_chat_agent,
                    is_approval=True, 
                    thread_id=chat_id, 
                    user_role=user_role
                )
                
            await context.bot.send_message(chat_id=chat_id, text=hasil["pesan"])
            
        elif query.data == 'cancel':
            await query.edit_message_text("❌ *Tindakan dibatalkan oleh user.*", parse_mode='Markdown')
            
    finally:
        USER_LOCKS[chat_id] = False # 🔓 Lepas Kunci

# ==========================================
# ⚙️ WORKER ANTREAN AI (THREAD 1)
# ==========================================
def queue_worker():
    print(f"[Worker AI] Thread pemrosesan berjalan...")
    while SERVICE_STATE["status"] == "RUNNING":
        try:
            file_path = cv_queue.get(timeout=2)
        except:
            continue
            
        filename = os.path.basename(file_path)
        SERVICE_STATE["current_file"] = filename 
        success = False
        
        for retry in range(5):
            try:
                hasil = process_cv(file_path)
                if hasil:
                    SERVICE_STATE["processed"] += 1
                else:
                    SERVICE_STATE["failed"] += 1
                success = True
                break
            except PermissionError:
                time.sleep(2)
            except Exception as e:
                SERVICE_STATE["failed"] += 1
                break
                
        SERVICE_STATE["current_file"] = "-" 
        cv_queue.task_done()

worker_thread = threading.Thread(target=queue_worker, daemon=True)
worker_thread.start()


# ==========================================
# 👁️ WATCHDOG MONITORING (THREAD 2)
# ==========================================
class HandlerWatchdog(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            ext = os.path.splitext(event.src_path)[1].lower()
            if ext in ALLOWED_EXTENSIONS:
                print(f"[Watchdog] File ditangkap: {os.path.basename(event.src_path)}")
                # Jeda sedikit agar OS selesai menyalin file utuh sebelum diproses AI
                time.sleep(1) 
                cv_queue.put(event.src_path)

observer = Observer()
observer.schedule(HandlerWatchdog(), path=str(UPLOAD_DIR), recursive=False)
observer.start()
print(f"[Watchdog] Memantau folder: {UPLOAD_DIR}")


# ==========================================
# 🤖 TELEGRAM BOT (THREAD 3)
# ==========================================
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    file_name = doc.file_name.lower()
    
    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ Ditolak! Ukuran maksimal 2MB.")
        return

    _, ext = os.path.splitext(file_name)
    if ext not in ALLOWED_EXTENSIONS:
        await update.message.reply_text(f"❌ Ekstensi '{ext}' tidak didukung.")
        return

    file = await doc.get_file()
    file_path = os.path.join(UPLOAD_DIR, doc.file_name)
    
    await update.message.reply_text(f"⏳ Mengunduh file '{doc.file_name}'...")
    await file.download_to_drive(file_path)
    
    # Begitu selesai didownload, file akan memicu Watchdog otomatis!
    await update.message.reply_text(f"✅ Berhasil diterima! Sistem AI sedang memproses dokumen tersebut.")

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Kirimkan CV/Draft sebagai 'Dokumen/File', bukan format lain.")

def start_telegram_bot():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app_bot = ApplicationBuilder().token(TG_TOKEN).build()
        
        # Daftarkan semua Handler (URUTANNYA PENTING)
        app_bot.add_handler(MessageHandler(filters.Document.ALL, handle_document, block=False)) # Upload file
        app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text, block=False)) # Chat Teks
        app_bot.add_handler(CallbackQueryHandler(handle_callback, block=False)) # Klik Tombol Inline
        app_bot.add_handler(MessageHandler(~filters.Document.ALL & ~filters.TEXT, handle_unknown, block=False))
        
        print("[Telegram Bot] Polling berjalan...")
        app_bot.run_polling(drop_pending_updates=True)
    except Exception as e:
        SERVICE_STATE["tg_bot_status"] = "ERROR 🔴"
        print(f"[Telegram Bot] Gagal berjalan: {e}")
tg_thread = threading.Thread(target=start_telegram_bot, daemon=True)
tg_thread.start()


# ==========================================
# 🌐 FLASK API & DASHBOARD (MAIN THREAD)
# ==========================================
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = str(UPLOAD_DIR)

@app.route('/api/upload', methods=['POST'])
def api_upload_cv():
    if 'file' not in request.files:
        return jsonify({"error": "Key 'file' tidak ditemukan"}), 400
        
    file = request.files['file']
    filename = secure_filename(file.filename)
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    file.save(save_path)
    # File disave -> Watchdog trigger otomatis
    return jsonify({"message": "Upload sukses", "file": filename}), 202

@app.route('/', methods=['GET'])
def halaman_monitor():
    # 1. Bersihkan chat yang sudah idle > 1 jam
    bersihkan_chat_lama()
    
    # 2. Bangun HTML untuk list sesi chat aktif
    html_chat_aktif = ""
    if not ACTIVE_CHATS:
        html_chat_aktif = "<div class='box' style='grid-column: span 2; color: #94a3b8; text-align:center;'>Tidak ada aktivitas chat dalam 1 jam terakhir.</div>"
    else:
        for cid, data in ACTIVE_CHATS.items():
            # Hitung sudah berapa menit yang lalu
            menit_lalu = int((time.time() - data['last_active']) / 60)
            waktu_teks = "Baru saja" if menit_lalu == 0 else f"{menit_lalu} mnt lalu"
            
            # Warna indikator status
            warna_status = "#2563eb" if "⏳" in data['status'] else "#16a34a" if "✅" in data['status'] else "#ca8a04"
            
            html_chat_aktif += f"""
            <div class="box" style="grid-column: span 2; display: flex; justify-content: space-between; text-align: left; align-items: center; border-left: 4px solid {warna_status};">
                <div>
                    <strong style="color: #0f172a;">{data['username']}</strong> <span style="font-size: 11px; color: #94a3b8;">(ID: {cid})</span><br>
                    <span style="font-size: 13px; color: #475569;">{data['status']}</span>
                </div>
                <div style="font-size: 12px; color: #64748b; background: #e2e8f0; padding: 4px 8px; border-radius: 12px;">
                    {waktu_teks}
                </div>
            </div>
            """

    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <title>Sistem Pusat AI HR</title>
        <style>
            :root {{ --primary: #2563eb; --bg: #f8fafc; --card: #ffffff; }}
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: var(--bg); margin: 0; padding: 40px 20px; color: #334155; }}
            .container {{ max-width: 600px; margin: auto; background: var(--card); padding: 30px; border-radius: 16px; box-shadow: 0 10px 15px -3px rgb(0 0 0 / 0.1); }}
            h2 {{ text-align: center; margin-top: 0; color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 15px; }}
            .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 20px; }}
            .box {{ background: #f1f5f9; padding: 15px; border-radius: 10px; text-align: center; border: 1px solid #e2e8f0; }}
            .box h3 {{ margin: 0 0 10px 0; font-size: 14px; color: #64748b; text-transform: uppercase; }}
            .box span {{ font-size: 24px; font-weight: bold; color: #0f172a; }}
            .active-job {{ background: #eff6ff; border-color: #bfdbfe; color: #1e3a8a; grid-column: span 2; }}
            .bot-status {{ background: #f0fdf4; border-color: #bbf7d0; grid-column: span 2; display: flex; justify-content: space-between; align-items: center; text-align: left; }}
            .footer {{ text-align: center; font-size: 12px; color: #94a3b8; margin-top: 20px; }}
        </style>
        <meta http-equiv="refresh" content="3">
    </head>
    <body>
        <div class="container">
            <h2>🧠 Control Panel AI HR</h2>
            
            <div class="grid">
                <div class="box bot-status">
                    <div>
                        <h3 style="margin:0; text-transform:none;">Telegram Bot Gateway</h3>
                        <small style="color: #64748b;">Menerima file dari chat Telegram</small>
                    </div>
                    <span style="font-size: 18px;">{SERVICE_STATE['tg_bot_status']}</span>
                </div>
                
                <div class="box active-job">
                    <h3>🔄 Sedang Diproses AI</h3>
                    <span>{SERVICE_STATE['current_file']}</span>
                </div>

                <div class="box">
                    <h3>⏳ Antrean</h3>
                    <span>{cv_queue.qsize()}</span>
                </div>
                <div class="box">
                    <h3>✅ Sukses</h3>
                    <span style="color: #16a34a;">{SERVICE_STATE['processed']}</span>
                </div>

                <!-- NEW: Panel Sesi Chat Aktif -->
                <div style="grid-column: span 2; margin-top: 10px;">
                    <h3 style="color: #0f172a; border-bottom: 1px solid #e2e8f0; padding-bottom: 8px; margin-bottom: 10px;">
                        💬 Sesi Chat Aktif (1 Jam Terakhir)
                    </h3>
                </div>
                {html_chat_aktif}
                <!-- End Sesi Chat Aktif -->
            </div>
            
            <div style="font-size: 13px; color: #64748b; background: #f8fafc; padding: 10px; border-radius: 6px;">
                <b>📂 Direktori Pantauan:</b><br> {app.config['UPLOAD_FOLDER']}
            </div>
            <div class="footer">Dashboard ini memuat ulang (refresh) otomatis setiap 3 detik.</div>
        </div>
    </body>
    </html>
    """
    return html

if __name__ == '__main__':
    print("\n[System] ===============================================")
    print("[System] Memulai Sistem Pusat Terpadu (Flask + Telegram)...")
    print("[System] Buka http://localhost:5000 untuk memantau.")
    print("[System] ===============================================\n")
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        SERVICE_STATE["status"] = "OFF"
        observer.stop()
        observer.join()
        print("\n[System] Semua layanan dimatikan dengan aman.")