from core_agent.registry import ToolRegistry

from .email_sendfunc import kirim_email_smtp
from .contact_osintfunc import pilih_kontak_terbaik
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


@ToolRegistry.register(category="safe")
def kirim_lamaran_ke_semua_kontak_tersimpan(
    provider: str,
    pengirim_email: str,
    nama_env_var_password: str,
    subjek: str,
    isi_email: str,
    perusahaan: str = "",
    url_lowongan: str = "",
    lampiran_path: str = "",
    lewati_yang_sudah_pernah_dikirim: bool = True,
) -> str:
    """Kirim email lamaran (CV) SEKALIGUS ke kontak-kontak yang SUDAH
    tersimpan di database hasil pencarian OSINT sebelumnya (tool ini
    TIDAK melakukan pencarian baru -- pakai data yang sudah ada).

    KURASI OTOMATIS: kalau satu orang punya beberapa kandidat email
    tebakan (pola berbeda / domain perusahaan+gmail+yahoo), tool ini
    CUMA memilih & mengirim ke SATU kandidat paling masuk akal per orang
    (prioritas: status_smtp 'valid' > domain perusahaan > domain
    pribadi Gmail/Yahoo yang 'tidak_pasti') -- BUKAN mengirim ke semua
    kandidat sekaligus. Ini penting: banyak kandidat cuma tebakan pola
    yang belum pasti benar, kirim ke semuanya bisa berujung spam ke
    orang-orang gmail/yahoo lain yang gak ada hubungannya sama lowongan.

    Kalau `lewati_yang_sudah_pernah_dikirim=True` (default), kontak yang
    sebelumnya SUDAH berhasil dikirimi (dicek dari riwayat_lamaran)
    otomatis dilewati, tidak dikirim ulang.

    Kalau pengiriman gagal karena masalah kredensial (env var password
    kosong/salah), proses BERHENTI LEBIH AWAL (tidak mencoba puluhan
    kontak lain dengan kesalahan konfigurasi yang sama) -- perbaiki dulu
    env var-nya baru panggil ulang.

    Args:
        provider: "gmail" atau "yahoo" (akun pengirim).
        pengirim_email: alamat email pribadi pelamar (akun pengirim).
        nama_env_var_password: NAMA environment variable tempat App
            Password akun pengirim disimpan (bukan password itu sendiri).
        subjek: subjek email, dipakai SAMA buat semua penerima (tool ini
            tidak melakukan mail-merge/personalisasi otomatis per nama).
        isi_email: isi/badan email (plain text), dipakai sama buat semua.
        perusahaan: filter cuma kirim ke kontak dari perusahaan ini
            (kosongkan buat kirim ke SEMUA kontak tersimpan, lintas
            semua perusahaan yang pernah dicari).
        url_lowongan: url lowongan terkait, dicatat di riwayat (opsional).
        lampiran_path: path file CV/lampiran di mesin yang jalanin agent.
        lewati_yang_sudah_pernah_dikirim: default True, biar gak dobel
            kirim ke kontak yang sama.
    """
    semua_kontak = cari_kontak(perusahaan=perusahaan, limit=1000)
    if not semua_kontak:
        target = f" untuk '{perusahaan}'" if perusahaan else ""
        return f"Tidak ada kontak tersimpan{target}. Jalankan tool pencarian kontak dulu."

    kontak_terpilih = pilih_kontak_terbaik(semua_kontak)

    hasil_kirim = []
    dilewati = 0

    for kontak in kontak_terpilih:
        penerima = kontak["email"]
        nama_perusahaan_ini = kontak["perusahaan"]

        if lewati_yang_sudah_pernah_dikirim and sudah_pernah_kirim(penerima, nama_perusahaan_ini):
            dilewati += 1
            continue

        berhasil, pesan_error = kirim_email_smtp(
            pengirim_email=pengirim_email,
            penerima_email=penerima,
            subjek=subjek,
            isi=isi_email,
            nama_env_var_password=nama_env_var_password,
            provider=provider,
            lampiran_path=lampiran_path,
        )

        id_riwayat = simpan_riwayat_kirim({
            "pengirim_email": pengirim_email,
            "penerima_email": penerima,
            "nama_perusahaan": nama_perusahaan_ini,
            "url_lowongan": url_lowongan,
            "subjek": subjek,
            "status_kirim": "berhasil" if berhasil else "gagal",
            "pesan_error": pesan_error,
        })

        hasil_kirim.append({
            "penerima": penerima, "perusahaan": nama_perusahaan_ini,
            "berhasil": berhasil, "id_riwayat": id_riwayat, "pesan_error": pesan_error,
        })

        # Masalah kredensial (bukan masalah 1 kontak spesifik) -> stop lebih awal
        if not berhasil and ("Environment variable" in pesan_error or "Autentikasi gagal" in pesan_error):
            break

    jumlah_berhasil = sum(1 for h in hasil_kirim if h["berhasil"])
    jumlah_gagal = sum(1 for h in hasil_kirim if not h["berhasil"])

    baris_detail = []
    for h in hasil_kirim:
        status_teks = "OK" if h["berhasil"] else f"GAGAL ({h['pesan_error']})"
        baris_detail.append(f"- {h['penerima']} ({h['perusahaan']}): {status_teks}")

    ringkasan = (
        f"Selesai. {jumlah_berhasil} terkirim, {jumlah_gagal} gagal, "
        f"{dilewati} dilewati (sudah pernah dikirim sebelumnya). "
        f"Dari {len(kontak_terpilih)} kontak terkurasi (hasil seleksi "
        f"dari total {len(semua_kontak)} baris kandidat tersimpan)."
    )
    return ringkasan + ("\n\n" + "\n".join(baris_detail) if baris_detail else "")