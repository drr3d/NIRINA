import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from database.job_db import simpan_lowongan

# ==========================================================================
# KONFIGURASI
# ==========================================================================
POLA_LINK_LOWONGAN = re.compile(r"/id/opportunities/jobs/([^/]+)/([0-9a-f-]{36})")
# Samakan dengan path_simpan default platform "glints" di
# session_login_func.PLATFORM_PRESET -- kalau salah satu diubah, ubah juga
# yang satunya.
AUTH_STATE_DEFAULT = "auth_glints.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ==========================================================================
# MODEL DATA
# ==========================================================================
@dataclass
class LowonganDetail:
    job_id: str
    url: str
    judul_pekerjaan: str = ""
    perusahaan: str = ""
    url_perusahaan: str = ""
    gaji: str = ""
    kategori: str = ""
    tipe_kerja: str = ""       # Penuh Waktu / Kontrak / Paruh Waktu / dll
    lokasi_kerja: str = ""     # Kerja di lokasi / Remote / Hybrid
    pendidikan_min: str = ""
    pengalaman_min: str = ""
    skills: list = field(default_factory=list)
    deskripsi_pekerjaan: str = ""
    industri_perusahaan: str = ""
    ukuran_perusahaan: str = ""
    website_perusahaan: str = ""
    deskripsi_perusahaan: str = ""
    alamat_kantor: str = ""
    dikelola_oleh: str = ""      # nama perekrut/HR -- diambil dari ManagedByContainer
    foto_pengelola: str = ""     # url foto profil perekrut (kalau ada)
    tag_pengelola: str = ""      # badge di sebelah nama, mis. "Perusahaan Premium" (opsional, sering kosong)
    tayang_pertama: str = ""
    diperbarui: str = ""
    tanggal_scrape: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

# ==========================================================================
# HELPER PARSING
# ==========================================================================
def _teks(el) -> str:
    return el.get_text(" ", strip=True) if el else ""

def _cari(soup, kelas_mengandung: str, tag: Optional[str] = None):
    return soup.find(tag, class_=re.compile(re.escape(kelas_mengandung)))

def _cari_semua(soup, kelas_mengandung: str, tag: Optional[str] = None):
    return soup.find_all(tag, class_=re.compile(re.escape(kelas_mengandung)))

def _parse_info_rows(soup) -> dict:
    """Parsing baris-baris 'JobOverViewInfo' (kategori, tipe kerja,
    pendidikan, pengalaman) -- baris ini urutannya bisa berubah, jadi
    diklasifikasi berdasarkan isi/ikon, bukan berdasarkan posisi."""
    hasil = {
        "kategori": "", "tipe_kerja": "", "lokasi_kerja": "",
        "pendidikan_min": "", "pengalaman_min": "",
    }

    container = _cari(soup, "JobOverViewInfoContainer", "div")
    if not container:
        return hasil

    for row in container.find_all("div", recursive=False):
        kelas_row = " ".join(row.get("class", []))
        # Lewati container badge/tombol yang ikut nested di sini
        if "BadgesAndJobOverviewTime" in kelas_row or "ButtonContainer" in kelas_row:
            continue
        if "SalaryJobOverview" in kelas_row:
            continue  # gaji sudah diambil terpisah

        teks_row = _teks(row)
        if not teks_row:
            continue

        svg_ikon = row.find("svg")
        kelas_svg = " ".join(svg_ikon.get("class", [])) if svg_ikon else ""

        if "GraduationHat" in kelas_svg:
            hasil["pendidikan_min"] = teks_row
        elif row.find("a", href=re.compile(r"/id/job-category/")):
            hasil["kategori"] = " > ".join(
                a.get_text(strip=True) for a in row.find_all("a")
            )
        elif "tahun" in teks_row.lower() and "pengalaman" in teks_row.lower():
            hasil["pengalaman_min"] = teks_row
        else:
            # sisanya: "Penuh Waktu · Kerja di lokasi"
            bagian = [b.strip() for b in teks_row.split("·")]
            if bagian:
                hasil["tipe_kerja"] = bagian[0]
            if len(bagian) > 1:
                hasil["lokasi_kerja"] = bagian[1]

    return hasil

def _parse_skills(soup) -> list:
    tag_container = _cari(soup, "Skillssc__TagContainer", "div")
    if not tag_container:
        return []
    nama_nama = _cari_semua(tag_container, "Skillssc__TagName", "p")
    return [n.get_text(strip=True) for n in nama_nama if n.get_text(strip=True)]

def _parse_deskripsi_pekerjaan(soup) -> str:
    artikel = soup.find("div", attrs={"aria-label": "Job Description"})
    if not artikel:
        return ""
    konten = _cari(artikel, "DraftjsReadersc__ContentContainer", "div")
    if not konten:
        return ""
    paragraf = [p.get_text(strip=True) for p in konten.find_all("p")]
    return "\n".join(p for p in paragraf if p)

def _parse_info_perusahaan(soup) -> dict:
    hasil = {
        "industri_perusahaan": "", "ukuran_perusahaan": "",
        "website_perusahaan": "", "deskripsi_perusahaan": "",
        "alamat_kantor": "",
    }

    blok = _cari(soup, "AboutCompanySectionsc__Main", "div")
    if not blok:
        return hasil

    industri_size = _cari(blok, "CompanyIndustryAndSize", "div")
    if industri_size:
        spans = [s.get_text(strip=True) for s in industri_size.find_all("span") if s.get_text(strip=True)]
        if spans:
            hasil["industri_perusahaan"] = spans[0]
        if len(spans) > 1:
            hasil["ukuran_perusahaan"] = spans[-1]

    website_tag = blok.find("a", attrs={"aria-label": "Company Website"})
    if website_tag:
        hasil["website_perusahaan"] = website_tag.get("href", "")

    desc_container = _cari(blok, "AboutCompanySectionsc__CompanyDesc", "div")
    if desc_container:
        hasil["deskripsi_perusahaan"] = _teks(desc_container)

    alamat_p = None
    alamat_wrapper = _cari(soup, "AboutCompanySectionsc__AddressWrapper", "div")
    if alamat_wrapper:
        alamat_p = alamat_wrapper.find("p")
    hasil["alamat_kantor"] = _teks(alamat_p)

    return hasil

def _parse_dikelola_oleh(soup) -> dict:
    """Parsing blok 'Loker ini dikelola oleh' (Opportunitysc__ManagedByContainer)
    -- berisi nama perekrut/HR yang pasang lowongan ini. Ini yang jadi bahan
    contact_osint_func.py buat cari kontak/email, jadi PENTING ditangkap.
    Bukan link/email langsung -- cuma nama + foto + badge opsional."""
    hasil = {"dikelola_oleh": "", "foto_pengelola": "", "tag_pengelola": ""}

    container = _cari(soup, "ManagedByContainer", "div")
    if not container:
        return hasil

    nama_tag = _cari(container, "CreatorName-", "div")
    hasil["dikelola_oleh"] = _teks(nama_tag)

    foto_tag = _cari(container, "CreatorImage", "img")
    if foto_tag:
        hasil["foto_pengelola"] = foto_tag.get("src", "")

    # Badge di sebelah nama (mis. "Perusahaan Premium") -- opsional, sering
    # tidak ada sama sekali, jangan dianggap wajib ada.
    tag_badge = _cari(container, "TagStyle__TagContent", "label")
    hasil["tag_pengelola"] = _teks(tag_badge)

    return hasil

def _parse_detail_html(html: str, url: str, job_id: str) -> LowonganDetail:
    soup = BeautifulSoup(html, "html.parser")

    judul = _teks(soup.find("h1", attrs={"aria-label": "Job Title"}))

    company_block = _cari(soup, "JobOverViewCompanyName", "div")
    company_a = company_block.find("a") if company_block else None
    perusahaan = _teks(company_a)
    url_perusahaan = company_a.get("href", "") if company_a else ""
    if url_perusahaan.startswith("/"):
        url_perusahaan = f"https://glints.com{url_perusahaan}"

    gaji_tag = _cari(soup, "BasicSalary", "span")
    gaji = _teks(gaji_tag)

    info_rows = _parse_info_rows(soup)
    skills = _parse_skills(soup)
    deskripsi_pekerjaan = _parse_deskripsi_pekerjaan(soup)
    info_perusahaan = _parse_info_perusahaan(soup)
    info_pengelola = _parse_dikelola_oleh(soup)

    tayang = _cari(soup, "PostedAt", "span")
    diperbarui = _cari(soup, "UpdatedAt", "span")

    return LowonganDetail(
        job_id=job_id,
        url=url,
        judul_pekerjaan=judul,
        perusahaan=perusahaan,
        url_perusahaan=url_perusahaan,
        gaji=gaji,
        skills=skills,
        deskripsi_pekerjaan=deskripsi_pekerjaan,
        tayang_pertama=_teks(tayang),
        diperbarui=_teks(diperbarui),
        **info_rows,
        **info_perusahaan,
        **info_pengelola,
    )

# ==========================================================================
# SCRAPING -- EXPLORE (index lowongan)
# ==========================================================================
def _scrape_explore(page, filters: dict, max_scrolls: int) -> list:
    base_url = "https://glints.com/id/opportunities/jobs/explore"
    query_string = urllib.parse.urlencode(filters)
    target_url = f"{base_url}?{query_string}"

    print(f"URL Target: {target_url}\n")
    page.goto(target_url, wait_until="networkidle")

    page.locator("body").click(position={"x": 10, "y": 10})

    print("Mulai melakukan scrolling...")
    for i in range(max_scrolls):
        page.keyboard.press("End")
        time.sleep(3)
        print(f"  -> Scroll ke-{i + 1} selesai.")

    soup = BeautifulSoup(page.content(), "html.parser")
    job_cards = soup.find_all("a", href=POLA_LINK_LOWONGAN)
    print(f"\nDitemukan {len(job_cards)} elemen lowongan potensial.\n")

    hasil_lowongan = []
    for card in job_cards:
        href = card.get("href")
        if "?" in href:
            href = href.split("?")[0]
        url_loker = f"https://glints.com{href}" if href.startswith("/") else href
        hasil_lowongan.append({"url": url_loker})

    hasil_unik = {job["url"]: job for job in hasil_lowongan}.values()
    return list(hasil_unik)

# ==========================================================================
# SCRAPING -- DETAIL (dibuka satu-satu)
# ==========================================================================
def _scrape_detail_batch(page, daftar_url: list, jeda_detik: float = 2.0) -> list:
    """Buka tiap url lowongan satu-satu, parsing halaman detailnya."""
    hasil = []
    total = len(daftar_url)

    for idx, item in enumerate(daftar_url, start=1):
        url = item["url"]
        match = POLA_LINK_LOWONGAN.search(url)
        if not match:
            print(f"  [{idx}/{total}] Lewati (url tidak sesuai pola): {url}")
            continue
        job_id = match.group(2)

        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_selector('h1[aria-label="Job Title"]', timeout=10000)
            html = page.content()
            detail = _parse_detail_html(html, url, job_id)
            hasil.append(detail)
            print(f"  [{idx}/{total}] OK  -> {detail.judul_pekerjaan or '(judul kosong)'}")
        except Exception as e:
            print(f"  [{idx}/{total}] GAGAL ({url}): {e}")
        finally:
            time.sleep(jeda_detik)

    return hasil

# ==========================================================================
# ORKESTRASI UTAMA (dipanggil dari tools_jobscrap.py)
# ==========================================================================
def jalankan_scrape_lowongan(
    filters: dict,
    max_scrolls: int,
    ambil_detail: bool,
    auth_state: str = AUTH_STATE_DEFAULT,
) -> list:
    """Jalankan scraping penuh (explore -> detail opsional) lalu simpan
    hasilnya ke database via job_db.simpan_lowongan(). Return list
    LowonganDetail yang berhasil discrape."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                storage_state=auth_state,
                user_agent=USER_AGENT,
            )
        except Exception:
            browser.close()
            raise RuntimeError(
                f"Gagal memuat '{auth_state}'. Pastikan sudah menjalankan "
                "script pembuat sesi login Glints terlebih dahulu."
            )

        page = context.new_page()
        daftar_url = _scrape_explore(page, filters, max_scrolls)

        if not ambil_detail:
            browser.close()
            hasil = []
            for item in daftar_url:
                match = POLA_LINK_LOWONGAN.search(item["url"])
                if match:
                    hasil.append(LowonganDetail(job_id=match.group(2), url=item["url"]))
            simpan_lowongan(hasil)
            return hasil

        print(f"\nMulai scrape detail untuk {len(daftar_url)} lowongan...\n")
        hasil_detail = _scrape_detail_batch(page, daftar_url)
        browser.close()

        simpan_lowongan(hasil_detail)
        return hasil_detail