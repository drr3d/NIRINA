from playwright.sync_api import sync_playwright

# Preset platform yang sudah dikenal, biar tool cukup disebut namanya saja
# tanpa perlu agent/user tahu persis url login & nama file auth-nya.
# Tambahkan entry baru di sini kalau ada platform lain yang mau didukung.
PLATFORM_PRESET = {
    "glints": {
        "url_login": "https://glints.com/id/login",
        "path_simpan": "auth_glints.json",
    },
    # "jobstreet": {"url_login": "...", "path_simpan": "auth_jobstreet.json"},
    # "linkedin":  {"url_login": "...", "path_simpan": "auth_linkedin.json"},
}


def resolve_path_simpan(platform: str, path_simpan: str = "") -> str:
    """Tentukan path file auth final, tanpa perlu url_login (buat kebutuhan
    cek-apakah-sesi-ada saja)."""
    platform_key = (platform or "").strip().lower()
    preset = PLATFORM_PRESET.get(platform_key, {})
    return path_simpan or preset.get("path_simpan", f"auth_{platform_key or 'sesi'}.json")


def resolve_target_login(platform: str, url_login: str = "", path_simpan: str = "") -> tuple:
    """Tentukan url_login & path_simpan final. Prioritas: parameter manual
    yang dikasih user > preset platform yang dikenal > error kalau
    platform tidak dikenal dan url_login tidak dikasih."""
    platform_key = (platform or "").strip().lower()
    preset = PLATFORM_PRESET.get(platform_key, {})

    url_final = url_login or preset.get("url_login", "")
    path_final = path_simpan or resolve_path_simpan(platform, path_simpan)

    if not url_final:
        raise ValueError(
            f"Platform '{platform}' belum dikenal dan url_login tidak diisi."
        )

    return url_final, path_final


def buat_sesi_login(url_login: str, path_simpan: str, headless: bool = False) -> str:
    """Buka browser, tunggu user login manual, lalu simpan cookies/token
    login (storage_state) ke file JSON.

    INI FUNGSI INTERAKTIF -- akan blocking menunggu ENTER di terminal
    tempat proses ini jalan, dan (kalau headless=False) butuh display
    untuk menampilkan browser. Cuma masuk akal dijalankan di mesin yang
    ada operator manusia standby, bukan di server headless tanpa
    pengawasan."""
    print(f"Membuka browser ke {url_login} ... Silakan login secara manual.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        page.goto(url_login)

        input(
            "Lakukan login di browser. Jika sudah berhasil masuk ke "
            "beranda, tekan ENTER di terminal ini..."
        )

        context.storage_state(path=path_simpan)
        browser.close()

    return path_simpan