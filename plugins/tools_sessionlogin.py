from pathlib import Path

from core_agent.registry import ToolRegistry

from .session_loginfunc import (
    buat_sesi_login,
    resolve_target_login,
    resolve_path_simpan,
    PLATFORM_PRESET,
)

@ToolRegistry.register(category="safe")
def buat_sesi_login_platform(
    platform: str,
    url_login: str = "",
    path_simpan: str = "",
) -> str:
    """Buka browser lokal dan MINTA USER LOGIN MANUAL ke suatu platform
    (Glints, LinkedIn, JobStreet, dll), lalu simpan sesi login (cookies)
    hasilnya ke file JSON supaya tool scraping lain bisa pakai sesi itu
    tanpa perlu login ulang.

    PENTING: fungsi ini INTERAKTIF -- akan membuka jendela browser dan
    menunggu user login manual (ada input() yang blocking terminal server
    tempat agent jalan). Cuma cocok dipanggil kalau ada operator manusia
    yang standby di depan mesin tempat agent jalan.

    Args:
        platform: nama platform, mis. "glints". Kalau platform sudah
            terdaftar di PLATFORM_PRESET (lihat session_login_func.py),
            url_login & path_simpan boleh dikosongkan.
        url_login: url halaman login. Wajib diisi kalau platform belum
            terdaftar di PLATFORM_PRESET.
        path_simpan: path file JSON tujuan penyimpanan sesi. Kalau kosong,
            dipakai default preset atau `auth_<platform>.json`.
    """
    try:
        url_final, path_final = resolve_target_login(platform, url_login, path_simpan)
    except ValueError as e:
        daftar_dikenal = ", ".join(PLATFORM_PRESET.keys()) or "(belum ada)"
        return f"{e} Platform yang sudah dikenal: {daftar_dikenal}."

    try:
        hasil_path = buat_sesi_login(url_final, path_final)
    except Exception as e:
        return f"Gagal membuat sesi login untuk '{platform}': {e}"

    return (
        f"Sesi login untuk '{platform}' berhasil disimpan ke '{hasil_path}'. "
        f"Tool scraping yang butuh sesi ini bisa langsung dipakai sekarang."
    )


@ToolRegistry.register(category="safe")
def cek_sesi_login_tersedia(platform: str, path_simpan: str = "") -> str:
    """Cek apakah file sesi login untuk suatu platform sudah ada. Ini
    cuma cek keberadaan file-nya, BUKAN validasi apakah sesinya masih
    hidup/belum expired -- kalau file ada tapi tetap gagal login saat
    scraping, kemungkinan sesinya sudah kedaluwarsa dan perlu dibuat ulang.

    Args:
        platform: nama platform, mis. "glints".
        path_simpan: path file JSON yang mau dicek. Kalau kosong, dipakai
            default preset platform tsb.
    """
    path_final = resolve_path_simpan(platform, path_simpan)
    if Path(path_final).exists():
        return f"Sesi login '{platform}' ditemukan di '{path_final}'."
    return (
        f"Sesi login '{platform}' BELUM ada di '{path_final}'. "
        f"Jalankan tool buat_sesi_login_platform dulu."
    )