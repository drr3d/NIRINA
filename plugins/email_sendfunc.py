import mimetypes
import os
import re
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_PRESET = {
    "gmail": {"host": "smtp.gmail.com", "port": 587},
    "yahoo": {"host": "smtp.mail.yahoo.com", "port": 587},
}

def ambil_password_dari_env(nama_env_var: str) -> str:
    """Ambil password dari environment variable APAPUN namanya (user
    yang set & yang tentuin namanya sendiri di OS-nya)."""
    return os.environ.get((nama_env_var or "").strip(), "")

def _validasi_email(alamat: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", alamat or ""))

def kirim_email_smtp(
    pengirim_email: str,
    penerima_email: str,
    subjek: str,
    isi: str,
    nama_env_var_password: str,
    provider: str = "gmail",
    lampiran_path: str = "",
    timeout: int = 20,
) -> tuple:
    """Kirim 1 email lewat SMTP akun pribadi (Gmail/Yahoo). Return
    (berhasil: bool, pesan: str) -- pesan berisi keterangan error kalau
    gagal, atau string kosong kalau berhasil. TIDAK PERNAH raise
    exception ke pemanggil -- semua kegagalan dikonversi ke pesan.

    Args:
        nama_env_var_password: NAMA environment variable tempat App
            Password disimpan (bukan password-nya sendiri), mis.
            "DPN2_GMAIL_APPPASS". User bebas kasih nama apa aja pas
            nge-set env var-nya sendiri di OS.
    """
    if not _validasi_email(pengirim_email):
        return False, f"Alamat pengirim '{pengirim_email}' tidak valid."
    if not _validasi_email(penerima_email):
        return False, f"Alamat penerima '{penerima_email}' tidak valid."

    preset = SMTP_PRESET.get((provider or "").strip().lower())
    if not preset:
        return False, f"Provider '{provider}' tidak didukung. Pilihan: {', '.join(SMTP_PRESET)}."

    if not nama_env_var_password:
        return False, "Nama environment variable buat password wajib diisi."

    password = ambil_password_dari_env(nama_env_var_password)
    if not password:
        return False, (
            f"Environment variable '{nama_env_var_password}' kosong/belum "
            "diset di mesin ini. Set dulu isinya dengan App Password akun "
            f"'{pengirim_email}' sebelum mengirim."
        )

    pesan = MIMEMultipart()
    pesan["From"] = pengirim_email
    pesan["To"] = penerima_email
    pesan["Subject"] = subjek
    pesan.attach(MIMEText(isi, "plain", "utf-8"))

    if lampiran_path:
        if not os.path.isfile(lampiran_path):
            return False, f"File lampiran '{lampiran_path}' tidak ditemukan."
        tipe, _ = mimetypes.guess_type(lampiran_path)
        maintype, subtype = (tipe or "application/octet-stream").split("/", 1)
        with open(lampiran_path, "rb") as f:
            lampiran = MIMEApplication(f.read(), _subtype=subtype)
        nama_file = os.path.basename(lampiran_path)
        lampiran.add_header("Content-Disposition", "attachment", filename=nama_file)
        pesan.attach(lampiran)

    try:
        with smtplib.SMTP(preset["host"], preset["port"], timeout=timeout) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(pengirim_email, password)
            smtp.sendmail(pengirim_email, penerima_email, pesan.as_string())
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, (
            "Autentikasi gagal -- pastikan isi environment variable "
            f"'{nama_env_var_password}' adalah App Password yang valid "
            "(bukan password akun biasa)."
        )
    except Exception as e:
        return False, f"Gagal mengirim: {e}"