import re
from typing import List, Callable

TELEGRAM_MSG_LIMIT = 4096
_SAFE_MARGIN = 200  # sisa ruang biar gak mepet limit pas nutup/buka code block

_HEADER_RE = re.compile(r'^(#{1,6})\s*(.+)$', re.MULTILINE)
_BULLET_RE = re.compile(r'^(\s*)[-*]\s+', re.MULTILINE)
_BOLD_GANDA_RE = re.compile(r'\*\*(.+?)\*\*')
_TABLE_ROW_RE = re.compile(r'^\s*\|.+\|\s*$')
_TABLE_SEP_RE = re.compile(r'^\s*\|[\s\-:|]+\|\s*$')

# Blok yang HARUS dilindungi dari transformasi apapun di bawah: code fence
# ```...``` (termasuk yang baru kita bikin dari tabel) dan inline `code`.
_PROTECTED_RE = re.compile(r'(```.*?```|`[^`\n]+`)', re.DOTALL)


def _is_protected(bagian: str) -> bool:
    return bagian.startswith("```") or (bagian.startswith("`") and bagian.endswith("`") and len(bagian) >= 2)


def _terapkan_di_luar_protected(text: str, fn: Callable[[str], str]) -> str:
    """Jalankan fn(potongan) HANYA ke bagian teks di luar ```code block```
    dan `inline code` -- supaya isi code/tabel yang sudah dirapikan gak
    ikut diutak-atik lagi oleh regex bold/bullet/header/neutralisasi."""
    bagian_bagian = _PROTECTED_RE.split(text)
    return "".join(p if _is_protected(p) else fn(p) for p in bagian_bagian)


def _netralkan_liar(text: str) -> str:
    """Buang sisa karakter markdown legacy (*, [, ]) yang gak berpasangan
    dari teks bebas, ganti underscore liar (mis. snake_case) jadi spasi --
    supaya parse_mode='Markdown' (yang GAK BISA di-escape sama sekali)
    gak pernah gagal gara-gara emphasis yang gak sengaja."""
    text = text.replace("_", " ")
    text = re.sub(r"[*`\[\]]", "", text)
    return re.sub(r" {2,}", " ", text)


def _proses_bagian_bebas(text: str) -> str:
    """
    Terapkan header->bold, **bold**->*bold*, bullet->titik, DAN netralisasi
    karakter liar -- dalam satu fungsi, dengan urutan yang benar.

    Kenapa harus digabung (bukan dipanggil terpisah berurutan): kalau
    netralisasi jalan belakangan sebagai langkah terpisah, dia akan ikut
    menghapus tanda '*' yang BARU SAJA kita pasang sendiri untuk
    header/bold (sama-sama karakter '*'). Makanya hasil header/bold
    "distash" dulu (dijadikan placeholder yang kebal terhadap netralisasi),
    baru netralisasi jalan ke sisa teks bebas, baru placeholder dikembalikan
    jadi *bold*/*header* yang sebenarnya di akhir.
    """
    token_tersimpan = []

    def _stash(sudah_aman: str) -> str:
        token_tersimpan.append(sudah_aman)
        return f"\x00K{len(token_tersimpan) - 1}\x00"

    # Header -> *Judul* (isi header dinetralkan dulu sebelum dibungkus '*',
    # jaga-jaga ada karakter liar juga di dalam teks headernya)
    text = _HEADER_RE.sub(lambda m: _stash(f"*{_netralkan_liar(m.group(2)).strip()}*"), text)

    # **bold** (gaya agent/CommonMark) -> *bold* (Telegram legacy cuma
    # kenal satu pasang bintang; dobel bakal berantakan/hilang formatnya)
    text = _BOLD_GANDA_RE.sub(lambda m: _stash(f"*{_netralkan_liar(m.group(1)).strip()}*"), text)

    # Bullet "- "/"* " di awal baris -> "• " (dilakukan SEBELUM netralisasi,
    # supaya '-'/'*' polos di awal baris gak keburu ilang)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}\u2022 ", text)

    # Netralkan sisa karakter markdown liar di teks bebas yang TERSISA
    # (placeholder \x00K<n>\x00 gak kesentuh karena isinya cuma huruf/angka)
    text = _netralkan_liar(text)

    # Kembalikan placeholder jadi *bold*/*header* yang sebenarnya
    for i, isi in enumerate(token_tersimpan):
        text = text.replace(f"\x00K{i}\x00", isi)

    return text


def _table_ke_monospace(text: str) -> str:
    """
    Baris berturut-turut '| a | b |' (markdown table) diubah jadi blok
    ``` dengan kolom DI-RATA (padding) berdasarkan lebar terpanjang tiap
    kolom -- karena font monospace, ini bikin tabel beneran sejajar,
    bukan cuma dump baris pipa mentah yang lebar kolomnya gak konsisten
    dari sumber aslinya.
    """
    lines = text.split("\n")
    out: List[str] = []
    buf_rows: List[List[str]] = []

    def flush():
        if not buf_rows:
            return
        ncols = max(len(r) for r in buf_rows)
        rows = [r + [""] * (ncols - len(r)) for r in buf_rows]
        lebar = [max(len(r[i]) for r in rows) for i in range(ncols)]
        out.append("```")
        for r in rows:
            out.append(" | ".join(c.ljust(lebar[i]) for i, c in enumerate(r)).rstrip())
        out.append("```")
        buf_rows.clear()

    for line in lines:
        if _TABLE_ROW_RE.match(line):
            if _TABLE_SEP_RE.match(line):  # baris pemisah header |---|---| -> skip
                continue
            sel = [c.strip() for c in line.strip().strip("|").split("|")]
            buf_rows.append(sel)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


def format_for_telegram(text: str) -> str:
    """
    Rapikan output markdown agent (gaya Streamlit) supaya enak dibaca DAN
    aman di-parse Telegram dengan parse_mode='Markdown'.
    """
    if not text:
        return text

    text = _table_ke_monospace(text)  # bikin blok ``` dulu, sebelum apa pun lain

    # Sisanya cuma boleh nyentuh teks DI LUAR blok terproteksi (``` dan
    # `inline code`), termasuk blok tabel yang barusan dibuat.
    text = _terapkan_di_luar_protected(text, _proses_bagian_bebas)

    text = re.sub(r"\n{3,}", "\n\n", text)  # rapikan baris kosong berturut-turut
    return text.strip()


def split_telegram_message(text: str, limit: int = TELEGRAM_MSG_LIMIT) -> List[str]:
    """
    Pecah pesan panjang jadi beberapa bagian <= limit, per BARIS (bukan
    cari titik potong lalu tempel ulang seperti versi sebelumnya) --
    supaya progresnya TERJAMIN maju tiap iterasi dan gak bisa nyangkut jadi
    infinite loop kalau titik potong kebetulan jatuh persis di penanda
    ``` yang baru dibuka ulang (kasus nyata: tabel/blok kode yang panjang).

    Blok ``` yang kepotong ke potongan berikutnya otomatis ditutup di akhir
    potongan ini & dibuka ulang di awal potongan berikutnya, biar formatnya
    gak 'bocor'.
    """
    if len(text) <= limit:
        return [text]

    batas = max(limit - _SAFE_MARGIN, 1)
    baris_list = text.split("\n")

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    di_dalam_fence = False

    def _fence_marker(baris: str) -> bool:
        return baris.strip().startswith("```")

    def _flush():
        nonlocal current, current_len
        if not current:
            return
        isi = "\n".join(current)
        if di_dalam_fence:
            isi += "\n```"
        isi = isi.strip("\n")
        if isi:
            chunks.append(isi)
        current = []
        current_len = 0

    def _mulai_potongan_baru():
        nonlocal current_len
        if di_dalam_fence:
            current.append("```")
            current_len = len("```") + 1

    for baris in baris_list:
        # Baris tunggal yang sendirian sudah melebihi batas (jarang, tapi
        # jangan sampai bikin loop gak maju) -> potong paksa per-karakter.
        if len(baris) > batas:
            for i in range(0, len(baris), batas):
                potong = baris[i:i + batas]
                if current and current_len + len(potong) + 1 > batas:
                    _flush()
                    _mulai_potongan_baru()
                current.append(potong)
                current_len += len(potong) + 1
            if _fence_marker(baris):
                di_dalam_fence = not di_dalam_fence
            continue

        tambahan = len(baris) + 1
        if current and current_len + tambahan > batas:
            _flush()
            _mulai_potongan_baru()

        current.append(baris)
        current_len += tambahan
        if _fence_marker(baris):
            di_dalam_fence = not di_dalam_fence

    _flush()
    return chunks