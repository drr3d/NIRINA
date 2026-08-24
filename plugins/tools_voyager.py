from core_agent.registry import ToolRegistry
from core_agent.agent_factory.factory_skilllib import defaults_kill_lib

@ToolRegistry.register(category="sensitive") 
def tools_reward(catatan_hasil: str, skor: int = 60) -> str:
    """
    KAMU DILARANG MEMANGGIL tool ini tanpa intruksi dari USER.
    Panggil HANYA kalau USER secara eksplisit bilang task sudah berhasil/selesai.
    Jika user memberikan penilaian/skor, masukkan ke argumen skor (misal: 80).
    Jika user tidak memberikan secara eksplisit skor/nilai, maka biarkan aja default nya 60.
    Jangan panggil ini atas inisiatif sendiri.
    """
    return f"[SINYAL: TASK BERHASIL] Skor: {skor}. Catatan: {catatan_hasil}"

@ToolRegistry.register(category="sensitive") 
def tools_gagal(catatan_hasil: str, skor: int = 0) -> str:
    """
    KAMU DILARANG MEMANGGIL tool ini tanpa intruksi dari USER.
    Panggil HANYA kalau USER bilang task ini dibatalkan/gagal total.
    Jika user memberikan penilaian/skor, masukkan ke argumen skor (misal: 80).
    Jika user tidak memberikan secara eksplisit skor/nilai, maka biarkan aja default nya 0.
    Jangan panggil ini atas inisiatif sendiri.
    """
    return f"[SINYAL: TASK GAGAL] Skor: {skor}. Catatan: {catatan_hasil}"

@ToolRegistry.register(category="sensitive") 
def tools_batal(alasan: str) -> str:
    """
    KAMU DILARANG MEMANGGIL tool ini tanpa intruksi dari USER.
    Panggil HANYA kalau USER secara eksplisit meminta untuk membatalkan, mereset, atau mengabaikan task yang sedang berjalan (atau task sebelumnya).
    Ini akan menghapus jejak memori task yang menggantung secara aman tanpa menyimpannya.
    """
    return f"[SINYAL: TASK DIBATALKAN & DIRESET] {alasan}"

@ToolRegistry.register(category="sensitive")
def lupakan_skill_gagal(nama_tool: str) -> str:
    """Hapus catatan skill GAGAL (anti-pattern) di Skill Library yang trace-nya
    menyebut nama_tool tertentu. Panggil tool ini SETELAH user memberi tahu
    bahwa sebuah tool yang sebelumnya bermasalah/rusak sudah diperbaiki --
    supaya AI berhenti mengutip kegagalan lama itu sebagai alasan menolak
    memanggil tool tersebut lagi.
 
    Args:
        nama_tool: nama PERSIS tool yang catatan gagalnya mau dihapus
                   (mis. "tanyakan_ke_openrouter"). Harus sama persis dengan
                   nama tool yang terdaftar di ToolRegistry.
    """
    jumlah = defaults_kill_lib.hapus_skill_terkait_tool(nama_tool, hanya_status="gagal")
    if jumlah == 0:
        return (
            f"Tidak ditemukan catatan skill GAGAL yang menyebut tool '{nama_tool}'. "
            f"Mungkin sudah kedaluwarsa duluan (lihat maks_umur_skill_gagal_detik), "
            f"atau memang belum pernah ada kegagalan tercatat untuk tool ini."
        )
    return f"Berhasil menghapus {jumlah} catatan skill GAGAL yang menyebut tool '{nama_tool}'. AI tidak akan lagi mengutip kegagalan lama itu."

@ToolRegistry.register(category="sensitive")
def atur_gorilla_tool_rag(aktif: bool) -> str:
    """Nyalakan atau matikan mekanisme Tool-RAG Gorilla-style (dynamic tool
    retrieval per giliran) UNTUK PERCAKAPAN INI SAJA, berlaku mulai giliran
    berikutnya -- tidak memengaruhi percakapan/user lain, dan tidak perlu
    restart proses.
 
    Panggil ini kalau user secara eksplisit minta "aktifkan/nonaktifkan
    gorilla rag", "matikan tool-rag", "nyalakan lagi tool-rag", atau kalimat
    senada. Saat DINONAKTIFKAN, SEMUA tool akan di-bind ke LLM setiap
    giliran (perilaku lama, tanpa filter) -- berguna buat debugging atau
    kalau user mencurigai Tool-RAG jadi penyebab masalah tertentu.
 
    Args:
        aktif: True untuk menyalakan Tool-RAG, False untuk mematikan.
    """
    status = "diaktifkan" if aktif else "dinonaktifkan"
    return (
        f"Tool-RAG Gorilla berhasil {status} untuk percakapan ini. "
        f"Berlaku mulai giliran berikutnya, tidak memengaruhi percakapan lain."
    )

@ToolRegistry.register(category="sensitive")
def minta_tool_manual(nama_tool: str) -> str:
    """
    Minta agar SATU tool tertentu (yang kamu tahu/ingat NAMANYA persis, tapi
    TIDAK muncul di daftar tool yang bisa kamu panggil sekarang) dipaksa
    ikut ter-bind mulai giliran BERIKUTNYA, untuk SISA task yang sedang
    berjalan.
 
    KAPAN PAKAI INI: kalau kamu YAKIN sebuah tool itu benar-benar ada
    (pernah kamu pakai sebelumnya di task ini/task lain, disebut di
    riwayat percakapan atau referensi skill library, atau user secara
    eksplisit menyebut nama tool itu) TAPI tool itu tidak ada di daftar
    tool yang bisa kamu panggil saat ini.
 
    JANGAN dipakai untuk menebak-nebak nama tool yang kamu TIDAK YAKIN
    benar-benar ada -- kalau namanya salah/tidak pernah terdaftar,
    permintaan ini akan DITOLAK (lihat isi balasannya), dan mengulang-ulang
    nama yang sama tidak akan mengubah hasilnya.
 
    Setelah tool yang diminta diterima (lihat isi balasan), tool itu BARU
    bisa kamu panggil mulai giliranmu SELANJUTNYA -- bukan di giliran yang
    sama dengan permintaan ini.
 
    Args:
        nama_tool: nama PERSIS tool yang kamu inginkan (case-sensitive,
            harus sama persis dengan nama tool aslinya).
    """
    # Validasi SESUNGGUHNYA (apakah tool ini genuinely ada, sudah pernah
    # diminta, atau sudah kena batas jumlah) terjadi di
    # GorillaToolSelector.proses_permintaan_tool_manual (dipanggil dari
    # agent_nodes.py setelah tool ini dieksekusi) -- di sinilah kita cuma
    # mengecek APAKAH tool itu ada, biar AI dapat feedback yang AKURAT lewat
    # ToolMessage balasan tool ini sendiri (bukan cuma lewat log server).
    nama_tool = (nama_tool or "").strip()
    nama_semua_tool = {t.name for t in ToolRegistry.get_all_tools()}
 
    if not nama_tool:
        return "❌ Nama tool kosong -- sebutkan nama tool yang kamu maksud."
 
    if nama_tool not in nama_semua_tool:
        return (
            f"❌ Tool '{nama_tool}' TIDAK DITEMUKAN di daftar tool yang "
            f"tersedia sama sekali (kemungkinan salah ketik, atau tool itu "
            f"memang tidak pernah terdaftar). JANGAN ulangi permintaan yang "
            f"sama -- cek lagi ejaan namanya, atau gunakan tool lain yang "
            f"memang ada."
        )
 
    return (
        f"✅ Tool '{nama_tool}' ditemukan. Tool itu akan dipaksa ikut "
        f"ter-bind mulai GILIRAN BERIKUTNYA untuk sisa task ini -- silakan "
        f"panggil tool itu langsung di giliranmu selanjutnya."
    )