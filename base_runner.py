from abc import ABC, abstractmethod
from typing import Any, Dict

class BaseAgentAdapter(ABC):
    """
    Kontrak resmi untuk channel baru (WhatsApp, Slack, CLI, dst). Semua
    channel yang sudah ada (Streamlit, Telegram, API) mengikuti pola yang
    sama:

        hasil = proses_chat_agent(user_input=..., thread_id=..., user_role=...)
        adapter.kirim_response(chat_id, hasil)

        # kalau user klik Setuju/Batal (atau hit endpoint /approve):
        hasil = proses_chat_agent(is_approval=True_atau_False, thread_id=..., user_role=...)
        adapter.kirim_response(chat_id, hasil)

    `chat_id` bebas tipe apapun -- itu identitas percakapan versi channel
    kamu (Telegram chat id, session id Streamlit, dst). Konvensi yang
    dipakai 3 channel bawaan: `thread_id` di proses_chat_agent() = str(chat_id)
    channel tersebut, jadi satu percakapan channel = satu thread agent.

    Implementasikan 3 method di bawah, lalu WAJIB pakai `kirim_response()`
    (sudah disediakan, jangan di-override kecuali channel kamu butuh urutan
    berbeda) sebagai satu-satunya titik panggil dari kode channel kamu.
    Dengan begitu kamu tidak perlu paham detail internal LangGraph state
    sama sekali -- cukup tau dict hasil proses_chat_agent() punya field
    status/pesan/tool/args/download_info.
    """

    @abstractmethod
    def kirim_teks(self, chat_id: Any, teks: str) -> None:
        """Kirim balasan teks biasa ke user di channel ini."""
        raise NotImplementedError

    @abstractmethod
    def kirim_permintaan_approval(self, chat_id: Any, response: Dict[str, Any]) -> None:
        """
        Dipanggil saat response["status"] == "butuh_persetujuan". `response`
        berisi: status, pesan (penjelasan tool yang mau dijalankan), tool
        (nama tool), args (argumen tool). Implementasi HARUS menyediakan
        cara user bilang setuju/batal -- lewat tombol (Telegram inline
        keyboard, Streamlit st.button), atau endpoint terpisah (API
        `/approve`). Setelah user memutuskan, channel kamu panggil ULANG
        proses_chat_agent(is_approval=True/False, thread_id=...) dan proses
        hasil barunya lewat kirim_response() lagi (bisa berulang kalau ada
        tool sensitif berantai).
        """
        raise NotImplementedError

    @abstractmethod
    def kirim_file(self, chat_id: Any, download_info: Dict[str, Any]) -> None:
        """
        Dipanggil saat response["download_info"] tidak None. Minimal berisi
        {"nama_file": ..., "path": ...} -- path ke file lokal yang harus
        dikirim/di-attach sesuai kemampuan channel (send_document di
        Telegram, st.download_button di Streamlit, base64/URL di API).
        """
        raise NotImplementedError

    def kirim_response(self, chat_id: Any, response: Dict[str, Any]) -> None:
        """
        Dispatcher default -- satu-satunya method yang dipanggil dari kode
        channel kamu setelah dapat dict dari proses_chat_agent(). Rute
        otomatis berdasarkan `status`. Override kalau channel kamu benar-benar
        butuh urutan/logic berbeda (jarang perlu).
        """
        status = response.get("status")

        if status == "error":
            self.kirim_teks(chat_id, f"⚠️ Terjadi kesalahan: {response.get('pesan', 'Unknown error')}")
            return

        if status == "butuh_persetujuan":
            self.kirim_permintaan_approval(chat_id, response)
            return

        pesan = response.get("pesan", "")
        if pesan:
            self.kirim_teks(chat_id, pesan)

        download_info = response.get("download_info")
        if download_info:
            self.kirim_file(chat_id, download_info)