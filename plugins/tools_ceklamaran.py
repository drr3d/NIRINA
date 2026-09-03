from core_agent.registry import ToolRegistry
from .statuslamaran import cari_lowongan_belum_dilamar, cek_status_lamaran_per_lowongan
 
@ToolRegistry.register(category="safe")
def cari_lowongan_belum_dilamar_tool(keyword: str = "", limit: int = 10) -> str:
    """Cari lowongan tersimpan yang cocok `keyword` (judul pekerjaan
    atau nama perusahaan) dan BELUM PERNAH berhasil dikirimi email
    lamaran, urut dari yang PALING BARU discrape. Cocok buat pertanyaan
    kayak "tampilkan lowongan X terakhir yang belum kita lamar".
 
    PENTING: hasil ini presisi per-lowongan (dicek by url), BUKAN cuma
    per-perusahaan -- kalau 1 perusahaan punya beberapa lowongan, yang
    ditampilkan cuma yang url-nya belum ada riwayat kirim SUKSES.
    Keakuratan ini bergantung pada `url_lowongan` yang diisi dengan
    benar tiap kali tool pengiriman email dipanggil sebelumnya.
 
    Args:
        keyword: filter judul pekerjaan/nama perusahaan (kosongkan
            untuk semua lowongan yang belum dilamar).
        limit: maksimal jumlah hasil.
    """
    rows = cari_lowongan_belum_dilamar(keyword=keyword, limit=limit)
    if not rows:
        target = f" untuk keyword '{keyword}'" if keyword else ""
        return (
            f"Tidak ada lowongan{target} yang belum dilamar -- baik "
            "karena semuanya sudah pernah dikirimi lamaran, atau memang "
            "belum ada lowongan yang cocok tersimpan sama sekali."
        )
 
    baris = []
    for r in rows:
        baris.append(
            f"- {r['judul_pekerjaan']} @ {r['perusahaan']} "
            f"({r['gaji'] or 'gaji tidak dicantumkan'}) "
            f"[{r['tipe_kerja']}, {r['lokasi_kerja']}]\n"
            f"  URL: {r['url']}\n"
            f"  Discrape: {r['tanggal_scrape']}"
        )
 
    return f"Ditemukan {len(rows)} lowongan yang belum dilamar:\n\n" + "\n".join(baris)
 
 
@ToolRegistry.register(category="safe")
def cek_status_lamaran_tool(url_lowongan: str) -> str:
    """Cek status lamaran buat SATU lowongan spesifik (by url) -- apa
    sudah pernah dikirim, kapan, dan ke mana. Berguna buat konfirmasi
    sebelum mengirim ulang.
 
    Args:
        url_lowongan: url lowongan yang mau dicek statusnya.
    """
    hasil = cek_status_lamaran_per_lowongan(url_lowongan)
    if not hasil:
        return f"Lowongan ini belum pernah dikirimi lamaran sama sekali."
    return (
        f"Terakhir dikirim ke {hasil['penerima_email']} pada "
        f"{hasil['waktu_kirim']} (status: {hasil['status_kirim']})."
    )
 