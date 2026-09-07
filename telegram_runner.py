import os
import time
import asyncio
import logging
from typing import Any, Dict, Coroutine

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
)

from core_agent.agent_runner import proses_chat_agent
from base_runner import BaseAgentAdapter
from telegram_formatter import format_for_telegram, split_telegram_message

logger = logging.getLogger(__name__)

# TODO: kalau butuh role-based access (mis. Admin vs Staff), petakan di sini
# berdasarkan Telegram user id. Default "Staff" untuk semua orang dulu.
TELEGRAM_USER_ROLES: Dict[int, str] = {
    # 123456789: "Admin",
}

def _thread_id_untuk(chat_id: int) -> str:
    """Satu chat Telegram = satu thread percakapan agent."""
    return f"telegram_{chat_id}"


def _role_untuk(user_id: int) -> str:
    return TELEGRAM_USER_ROLES.get(user_id, "Staff")


# ==========================================
# 🛡️ ANTI-SPAM LOCK (dengan timeout)
# ==========================================
USER_LOCKS: Dict[int, float] = {}   # chat_id -> timestamp mulai proses
# Disesuaikan buat spek lokal (bukan production) -- LLM lokal bisa butuh
# waktu lama. AI_HARD_TIMEOUT * MAX_WAIT_LOOPS = 60 * 20 = 1200s = 20 menit,
# selaras sama AGENT_API_TIMEOUT di agent_client.py (juga 1200s) supaya
# gak ada yang nyerah duluan sebelum yang lain. LOCK_STALE_AFTER WAJIB lebih
# besar dari totalnya (1200s) -- kalau lebih kecil, lock bakal dianggap basi
# padahal request masih sah berjalan, dan user bisa ngirim pesan baru yang
# numpuk ke thread yang sama selagi masih diproses.
LOCK_STALE_AFTER = 1260              # detik (21 menit) -- harus > total AI_HARD_TIMEOUT*MAX_WAIT_LOOPS
AI_HARD_TIMEOUT = 60                 # detik. Batas keras nunggu proses_chat_agent tiap iterasi.
MAX_WAIT_LOOPS = 20                   # Maksimal perulangan peringatan "Harap tunggu" (60*20=1200s=20 menit)


def _is_locked(chat_id: int) -> bool:
    """True kalau chat masih terkunci DAN lock-nya belum basi."""
    ts = USER_LOCKS.get(chat_id)
    if ts is None:
        return False
    if time.time() - ts > LOCK_STALE_AFTER:
        logger.warning(f"[Lock] chat_id={chat_id} lock basi (>{LOCK_STALE_AFTER}s), dilepas otomatis.")
        USER_LOCKS.pop(chat_id, None)
        return False
    return True


def _acquire_lock(chat_id: int) -> None:
    USER_LOCKS[chat_id] = time.time()


def _release_lock(chat_id: int) -> None:
    USER_LOCKS.pop(chat_id, None)


# Cooldown biar peringatan "mohon tunggu" gak ikut jadi spam kalau user
# ngirim banyak pesan beruntun selagi locked -- pesan spam tetap SELALU
# diabaikan (gak diproses), ini cuma ngerem SERINGNYA balasan peringatan.
_LAST_WARNED: Dict[int, float] = {}
WARN_COOLDOWN = 5  # detik


def _boleh_kirim_peringatan(chat_id: int) -> bool:
    now = time.time()
    if now - _LAST_WARNED.get(chat_id, 0) >= WARN_COOLDOWN:
        _LAST_WARNED[chat_id] = now
        return True
    return False


class TelegramAdapter(BaseAgentAdapter):
    """
    Implementasi BaseAgentAdapter untuk Telegram. `context` (dari
    python-telegram-bot) disimpan supaya bisa dipakai kirim pesan/file di
    luar alur update langsung (mis. dari callback approval).
    """
    def __init__(self, context: ContextTypes.DEFAULT_TYPE):
        self.context = context

    def kirim_teks(self, chat_id: Any, teks: str) -> None:
        teks_rapi = format_for_telegram(teks)
        potongan = split_telegram_message(teks_rapi)

        async def _kirim():
            for bagian in potongan:
                try:
                    # 1. Coba kirim dengan Markdown
                    await self.context.bot.send_message(chat_id=chat_id, text=bagian, parse_mode="Markdown")
                except BadRequest as e:
                    # 2. Tangkap error HANYA JIKA error-nya karena format Markdown berantakan
                    if "parse entities" in str(e).lower():
                        logger.warning(f"[Markdown Fallback] chat_id={chat_id} gagal parse, kirim ulang sbg teks biasa.")
                        # Kirim ulang TANPA parse_mode (sebagai plain text yang 100% aman)
                        await self.context.bot.send_message(chat_id=chat_id, text=bagian)
                    else:
                        # Jika error lain (misal koneksi putus), biarkan error muncul
                        raise e

        self.context.application.create_task(_kirim())

    def kirim_permintaan_approval(self, chat_id: Any, response: Dict[str, Any]) -> None:
        keyboard = [
            [
                InlineKeyboardButton("✅ Setujui & Lanjutkan", callback_data=f"approve:{chat_id}"),
                InlineKeyboardButton("❌ Batalkan", callback_data=f"cancel:{chat_id}"),
            ]
        ]
        markup = InlineKeyboardMarkup(keyboard)
        teks_rapi = format_for_telegram(response.get("pesan", "AI membutuhkan persetujuan Anda."))
        potongan = split_telegram_message(teks_rapi)

        async def _kirim():
            for i, bagian in enumerate(potongan):
                is_terakhir = i == len(potongan) - 1
                try:
                    await self.context.bot.send_message(
                        chat_id=chat_id,
                        text=bagian,
                        reply_markup=markup if is_terakhir else None,
                        parse_mode="Markdown",
                    )
                except BadRequest as e:
                    if "parse entities" in str(e).lower():
                        logger.warning(f"[Markdown Fallback Approval] chat_id={chat_id} gagal parse.")
                        await self.context.bot.send_message(
                            chat_id=chat_id,
                            text=bagian,
                            reply_markup=markup if is_terakhir else None,
                            # parse_mode dihilangkan di sini
                        )
                    else:
                        raise e

        self.context.application.create_task(_kirim())

    def kirim_file(self, chat_id: Any, download_info: Dict[str, Any]) -> None:
        path = download_info.get("path")
        nama_file = download_info.get("nama_file", "file")
        if not path or not os.path.exists(path):
            self.kirim_teks(chat_id, f"⚠️ File '{nama_file}' seharusnya siap tapi tidak ditemukan di server.")
            return
        self.context.application.create_task(
            self.context.bot.send_document(chat_id=chat_id, document=open(path, "rb"), filename=nama_file)
        )


# ==========================================
# ⚙️ HELPER: PENUNGGUAN DENGAN LOOP (SHIELD)
# ==========================================
async def _proses_dengan_tunggu(chat_id: int, adapter: TelegramAdapter, coro: Coroutine) -> Dict[str, Any]:
    """
    Membungkus eksekusi ke LLM (coroutine) ke dalam Task dan melindunginya
    dari pembatalan (shield) saat timeout terpicu. Jika timeout, kirim pesan
    peringatan lalu lanjutkan menunggu task yang sama.

    Kalau sampai MAX_WAIT_LOOPS habis (AI beneran gak jawab-jawab), ai_task
    dibatalkan di sini SEBELUM raise -- supaya thread/koneksi LLM yang
    nyangkut tidak terus jalan tanpa ada yang nungguin di background, dan
    gak ninggalin "Task exception was never retrieved" di log.
    """
    # 1. Jadikan coroutine sebagai Task tunggal
    ai_task = asyncio.create_task(coro)

    # 2. Loop penungguan
    for loop_idx in range(MAX_WAIT_LOOPS):
        try:
            # Gunakan asyncio.shield agar ai_task tidak dibunuh oleh wait_for
            hasil = await asyncio.wait_for(asyncio.shield(ai_task), timeout=AI_HARD_TIMEOUT)
            return hasil  # Sukses, kembalikan hasil

        except asyncio.TimeoutError:
            if loop_idx < MAX_WAIT_LOOPS - 1:
                logger.info(f"[Wait] chat_id={chat_id} AI butuh waktu ekstra (loop {loop_idx+1}/{MAX_WAIT_LOOPS}).")
                adapter.kirim_teks(chat_id, "⏳ _AI masih menyusun jawaban, harap tunggu sebentar..._")
            else:
                # Batas maksimal loop tercapai -> lepas ai_task, jangan biarkan
                # nyangkut jalan sendirian tanpa ada yang nungguin hasilnya.
                logger.error(f"[Wait] chat_id={chat_id} melebihi MAX_WAIT_LOOPS, membatalkan ai_task.")
                ai_task.cancel()
                try:
                    await ai_task
                except (asyncio.CancelledError, Exception):
                    # Baik dibatalkan bersih maupun sempat lempar error lain saat
                    # dibatalkan, sama-sama diredam di sini -- kita sudah nyerah
                    # nunggu, jadi hasil task ini sudah tidak relevan lagi.
                    pass
                raise asyncio.TimeoutError()

    return {}  # Fallback logis (harusnya tidak pernah tercapai)


# ==========================================
# HANDLERS
# ==========================================
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Halo! Tanya apa saja soal data kamu di sini.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    prompt = update.message.text

    adapter = TelegramAdapter(context)

    # 🛡️ Cek lock (anti-spam / anti double-submit)
    if _is_locked(chat_id):
        # Pesan spam TETAP selalu diabaikan (gak pernah diproses) apapun
        # hasil _boleh_kirim_peringatan -- cooldown ini cuma ngerem SERINGNYA
        # balasan "mohon tunggu", biar Telegram gak ikut kebanjiran balasan
        # kalau user ngirim 10 pesan beruntun dalam beberapa detik.
        if _boleh_kirim_peringatan(chat_id):
            pesan_spam = await update.message.reply_text(
                "⚠️ Mohon tunggu, jawaban sebelumnya masih diproses..."
            )
            await asyncio.sleep(2)
            try:
                await pesan_spam.delete()
            except Exception:
                pass
        return

    _acquire_lock(chat_id)
    try:
        # Gunakan helper baru untuk loop & shield
        hasil = await _proses_dengan_tunggu(
            chat_id,
            adapter,
            asyncio.to_thread(
                proses_chat_agent,
                user_input=prompt,
                thread_id=_thread_id_untuk(chat_id),
                user_role=_role_untuk(user_id),
            )
        )
        adapter.kirim_response(chat_id, hasil)

    except asyncio.TimeoutError:
        total_batas = AI_HARD_TIMEOUT * MAX_WAIT_LOOPS
        logger.error(f"[Timeout] chat_id={chat_id} AI tidak merespons dalam {total_batas}s total")
        adapter.kirim_teks(
            chat_id,
            "⌛ AI membutuhkan waktu terlalu lama dan tidak merespons. "
            "Silakan coba kirim ulang pertanyaanmu atau ringkas perintahmu."
        )
    except Exception:
        logger.exception(f"[Error] chat_id={chat_id}")
        adapter.kirim_teks(chat_id, "❌ Terjadi kesalahan saat memproses pesanmu.")

    finally:
        _release_lock(chat_id)


async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # wajib, biar loading spinner tombol Telegram hilang

    aksi, chat_id_str = query.data.split(":", 1)
    chat_id = int(chat_id_str)
    user_id = update.effective_user.id
    setuju = (aksi == "approve")

    adapter = TelegramAdapter(context)

    # 🛡️ Abaikan klik dobel selagi proses sebelumnya (masih) berjalan
    if _is_locked(chat_id):
        return

    _acquire_lock(chat_id)
    try:
        if setuju:
            # Gunakan helper baru
            hasil = await _proses_dengan_tunggu(
                chat_id,
                adapter,
                asyncio.to_thread(
                    proses_chat_agent,
                    is_approval=True,
                    thread_id=_thread_id_untuk(chat_id),
                    user_role=_role_untuk(user_id),
                )
            )

            nama_tool_disetujui = None
            while hasil.get("status") == "butuh_persetujuan":
                if nama_tool_disetujui is not None and hasil.get("tool") != nama_tool_disetujui:
                    break
                nama_tool_disetujui = hasil.get("tool")

                # Gunakan helper baru untuk repetisi persetujuan
                hasil = await _proses_dengan_tunggu(
                    chat_id,
                    adapter,
                    asyncio.to_thread(
                        proses_chat_agent,
                        is_approval=True,
                        thread_id=_thread_id_untuk(chat_id),
                        user_role=_role_untuk(user_id),
                    )
                )
        else:
            # Gunakan helper baru untuk penolakan
            hasil = await _proses_dengan_tunggu(
                chat_id,
                adapter,
                asyncio.to_thread(
                    proses_chat_agent,
                    is_approval=False,
                    user_input=(
                        "[SYSTEM] User membatalkan aksi tool tadi. "
                        "Jangan ulangi tool yang sama - tanyakan instruksi "
                        "lanjutan ke user, atau hentikan proses ini kalau "
                        "memang sudah tidak relevan."
                    ),
                    thread_id=_thread_id_untuk(chat_id),
                    user_role=_role_untuk(user_id),
                )
            )

        # Hapus tombol approval lama biar gak diklik dobel
        await query.edit_message_reply_markup(reply_markup=None)
        adapter.kirim_response(chat_id, hasil)

    except asyncio.TimeoutError:
        total_batas = AI_HARD_TIMEOUT * MAX_WAIT_LOOPS
        logger.error(f"[Timeout/callback] chat_id={chat_id} AI tidak merespons dalam {total_batas}s total")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        adapter.kirim_teks(chat_id, "⌛ Proses persetujuan memakan waktu terlalu lama. Silakan coba lagi nanti.")
    except Exception:
        logger.exception(f"[Error/callback] chat_id={chat_id}")
        adapter.kirim_teks(chat_id, "❌ Terjadi kesalahan saat memproses persetujuan.")

    finally:
        _release_lock(chat_id)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set env var TELEGRAM_BOT_TOKEN dulu (dari @BotFather).")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_approval_callback))

    print("🤖 Telegram bot jalan, menunggu pesan...")
    app.run_polling()


if __name__ == "__main__":
    main()