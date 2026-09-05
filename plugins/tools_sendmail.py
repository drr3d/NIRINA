from core_agent.registry import ToolRegistry

from .email_sendfunc import kirim_email_smtp
from database.sendmail_db import simpan_riwayat_kirim, cari_riwayat_kirim, sudah_pernah_kirim
from database.contact_db import cari_kontak

@ToolRegistry.register(category="safe")
def kirim_lamaran_email(
    provider: str,
    pengirim_email: str,
    penerima_email: str,
    nama_perusahaan: str,
    subjek: str,
    isi_email: str,
    nama_env_var_password: str,
    url_lowongan: str = "",
    lampiran_path: str = "",
) -> str:
    """Kirim email lamaran ke satu alamat, pakai akun email PRIBADI
    pelamar (Gmail atau Yahoo). Setiap pengiriman (berhasil ATAU gagal)
    otomatis dicatat ke riwayat -- siapa penerimanya, buat perusahaan/
    lowongan apa, kapan, dan statusnya.

    SOAL PASSWORD: tool ini TIDAK PERNAH menerima password sebagai
    argumen -- yang diminta cuma NAMA environment variable tempat
    password itu disimpan di mesin yang jalanin agent (nama bebas, user
    yang nentuin sendiri pas nge-set env var-nya, mis.
    "DPN2_GMAIL_APPPASS"). Kalau env var itu belum diset/kosong,
    pengiriman akan gagal dengan pesan yang jelas -- JANGAN pernah minta
    user ketik isi password-nya lewat chat, itu bukan tugas tool ini.

    Args:
        provider: "gmail" atau "yahoo".
        pengirim_email: alamat email pribadi pelamar (akun pengirim).
        penerima_email: alamat email tujuan (mis. hasil tool OSINT
            kontak sebelumnya, atau alamat ATS/HR resmi).
        nama_perusahaan: nama perusahaan tujuan lamaran (buat pencatatan
            riwayat, bukan bagian isi email).
        subjek: subjek email.
        isi_email: isi/badan email (plain text).
        nama_env_var_password: NAMA environment variable tempat App
            Password akun pengirim disimpan (bukan password-nya
            sendiri), mis. "DPN2_GMAIL_APPPASS".
        url_lowongan: url lowongan terkait (opsional, buat jejak riwayat
            -- supaya nanti bisa dicek "sudah pernah kirim buat lowongan
            ini belum" lewat `lihat_riwayat_lamaran`).
        lampiran_path: path file lampiran (mis. CV/portofolio) di mesin
            yang jalanin agent, opsional.
    """
    peringatan_duplikat = ""
    riwayat_lama = sudah_pernah_kirim(penerima_email, nama_perusahaan)
    if riwayat_lama:
        peringatan_duplikat = (
            f"\n[PERHATIAN] Sudah pernah berhasil kirim ke alamat ini "
            f"untuk '{nama_perusahaan}' pada {riwayat_lama['waktu_kirim']} "
            f"(subjek: '{riwayat_lama['subjek']}'). Ini tetap dikirim ulang."
        )

    berhasil, pesan_error = kirim_email_smtp(
        pengirim_email=pengirim_email,
        penerima_email=penerima_email,
        subjek=subjek,
        isi=isi_email,
        nama_env_var_password=nama_env_var_password,
        provider=provider,
        lampiran_path=lampiran_path,
    )

    id_riwayat = simpan_riwayat_kirim({
        "pengirim_email": pengirim_email,
        "penerima_email": penerima_email,
        "nama_perusahaan": nama_perusahaan,
        "url_lowongan": url_lowongan,
        "subjek": subjek,
        "status_kirim": "berhasil" if berhasil else "gagal",
        "pesan_error": pesan_error,
    })

    if berhasil:
        return (
            f"Email lamaran berhasil dikirim ke {penerima_email} "
            f"({nama_perusahaan}). Dicatat sebagai riwayat #{id_riwayat}."
            f"{peringatan_duplikat}"
        )
    return (
        f"Email lamaran ke {penerima_email} ({nama_perusahaan}) GAGAL "
        f"dikirim: {pesan_error}. Tetap dicatat sebagai riwayat #{id_riwayat} "
        f"(status gagal).{peringatan_duplikat}"
    )


@ToolRegistry.register(category="safe")
def lihat_riwayat_lamaran(perusahaan: str = "", penerima: str = "", limit: int = 20) -> str:
    """Tampilkan riwayat pengiriman email lamaran (kapan, ke siapa,
    perusahaan/lowongan apa, berhasil atau gagal). Berguna buat cek
    lowongan mana yang sudah/belum pernah dilamar, atau follow-up,
    SEBELUM memutuskan mengirim lamaran baru.

    Args:
        perusahaan: filter nama perusahaan (kosongkan untuk semua).
        penerima: filter alamat email penerima (kosongkan untuk semua).
        limit: maksimal jumlah hasil.
    """
    rows = cari_riwayat_kirim(perusahaan=perusahaan, penerima=penerima, limit=limit)
    if not rows:
        return "Belum ada riwayat pengiriman lamaran yang cocok."

    baris = []
    for r in rows:
        status = r["status_kirim"].upper()
        ket_error = (
            f" ({r['pesan_error']})"
            if r["status_kirim"] == "gagal" and r["pesan_error"] else ""
        )
        baris.append(
            f"- [{status}] {r['waktu_kirim']} -> {r['penerima_email']} "
            f"({r['nama_perusahaan']}), subjek: '{r['subjek']}'{ket_error}"
        )

    return f"Ditemukan {len(rows)} riwayat pengiriman:\n\n" + "\n".join(baris)