from core_agent.registry import ToolRegistry

from .job_scrapefunc import jalankan_scrape_lowongan, AUTH_STATE_DEFAULT
from database.job_db import cari_lowongan

@ToolRegistry.register(category="safe")
def scrape_lowongan_glints(
    keyword: str,
    lokasi: str = "All Cities/Provinces",
    max_scrolls: int = 2,
    scrape_detail: bool = True,
) -> str:
    """Scrape lowongan kerja dari Glints berdasarkan keyword & lokasi, lalu
    simpan hasilnya ke database.

    Gunakan tool ini kalau user minta dicarikan lowongan kerja BARU dari
    Glints (bukan dari data yang sudah tersimpan -- untuk itu pakai
    `cari_lowongan_tersimpan`).

    Args:
        keyword: kata kunci posisi/skill, mis. "software engineer".
        lokasi: nama lokasi Glints, default "All Cities/Provinces".
        max_scrolls: berapa kali scroll di halaman explore (makin banyak,
            makin banyak lowongan yang ke-load, tapi makin lama).
        scrape_detail: kalau True, tiap lowongan dibuka satu-satu untuk
            ambil detail lengkap (gaji, syarat, skills, deskripsi, dll).
            Kalau False, cuma simpan url-nya saja (lebih cepat).
    """
    filters = {
        "keyword": keyword,
        "country": "ID",
        "locationName": lokasi,
        "lowestLocationLevel": "1",
    }

    try:
        hasil = jalankan_scrape_lowongan(
            filters=filters,
            max_scrolls=max_scrolls,
            ambil_detail=scrape_detail,
            auth_state=AUTH_STATE_DEFAULT,
        )
    except Exception as e:
        return f"Scraping gagal: {e}"

    if not hasil:
        return f"Tidak ada lowongan ditemukan untuk keyword '{keyword}'."

    ringkas = [
        f"- {d.judul_pekerjaan or '(tanpa judul)'} @ {d.perusahaan or '(?)'}"
        f"{f' | {d.gaji}' if d.gaji else ''}"
        for d in hasil[:15]
    ]
    lebih = f"\n... dan {len(hasil) - 15} lowongan lain." if len(hasil) > 15 else ""

    return (
        f"Berhasil scrape {len(hasil)} lowongan untuk keyword '{keyword}' "
        f"dan disimpan ke database.\n\nContoh hasil:\n"
        + "\n".join(ringkas) + lebih
    )


@ToolRegistry.register(category="safe")
def cari_lowongan_tersimpan(keyword: str = "", limit: int = 10) -> str:
    """Cari/tampilkan lowongan kerja yang SUDAH tersimpan di database hasil
    scrape sebelumnya (tidak melakukan scraping baru). Gunakan ini untuk
    menampilkan pilihan lowongan ke pelamar.

    Args:
        keyword: filter judul pekerjaan/nama perusahaan (kosongkan untuk
            tampilkan semua, urut dari yang paling baru discrape).
        limit: maksimal jumlah hasil yang ditampilkan.
    """
    rows = cari_lowongan(keyword=keyword, limit=limit)

    if not rows:
        pesan_kw = f" yang cocok dengan '{keyword}'" if keyword else ""
        return f"Tidak ada lowongan tersimpan{pesan_kw}."

    baris_teks = []
    for r in rows:
        baris_teks.append(
            f"- {r['judul_pekerjaan']} di {r['perusahaan']} "
            f"({r['gaji'] or 'gaji tidak dicantumkan'}) "
            f"[{r['tipe_kerja']}, {r['lokasi_kerja']}, "
            f"min. {r['pendidikan_min'] or '-'}, {r['pengalaman_min'] or '-'}]\n"
            f"  Link: {r['url']}"
        )

    return f"Ditemukan {len(rows)} lowongan:\n\n" + "\n".join(baris_teks)