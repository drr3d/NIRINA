"""
Entity dictionary: real names (e.g. customer names a bot deals with) are stored as SALTED HASHES, never as text.

Used to:
- scan code/text for real names that were written down by accident (e.g. in a repo test);
- recognise names in user messages without writing those names in code (bot gate);
- mask text before it is stored or shared (`samarkan`), e.g. an exported skill.

Generic add-on (outside core_agent): the kinds of entities and the source of their names are decided by the
bot's own code. The dictionary file lives outside the repo (default `<data_dir>/kamus_entitas.json`, where
data_dir is `core_agent.config.data_dir` / env NIRINA_DATA_DIR; override with env `NIRINA_KAMUS_ENTITAS`)
and never contains the real names.

Matching: lower case, accents removed, ALL punctuation is a word separator ("Acme's" = Acme + apostrophe + s =
"acme s", "Widget-Kids" = "Widget Kids"), and the form without spaces is recognised too ("WidgetWorks",
"widget works" = name "Widget Works"; "Unit 5" = name "Unit5"). Digits that are part of a number
("42,000") are not the name "42".
Note: a short name can still be guessed from its hash (try every word), so the dictionary file stays secret.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from core_agent.config import app_dir, data_dir

VERSI = 2
MAKS_KATA_GABUNG = 4            # "widget works" (2 kata) dicocokkan juga sebagai "widgetworks"
MIN_HURUF_GABUNG = 4            # bentuk gabungan sependek ini tidak dicoba (terlalu banyak kebetulan)
_TANDA = chr(0x300) + "-" + chr(0x36F)                              # tanda aksen terpisah (bentuk NFD)
_KATA = re.compile(f"(?:[^\\W_]|[{_TANDA}])+")                       # huruf/angka semua aksara
_ESCAPE = re.compile(r"\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|[nrtfv0])")   # escape di literal kode
_PERSEN = re.compile(r"%[0-9a-fA-F]{2}")                                  # URL-encode ("%20")


def lokasi_bawaan() -> Path:
    return Path(os.environ.get("NIRINA_KAMUS_ENTITAS") or (data_dir / "kamus_entitas.json"))


def _bentuk(kata: str) -> str:
    """Satu kata -> bentuk pembanding: NFKD, tanda aksen dibuang, huruf kecil."""
    t = unicodedata.normalize("NFKD", kata)
    return "".join(c for c in t if not unicodedata.combining(c)).casefold()


def normalisasi(teks: str) -> str:
    """Huruf kecil, tanpa aksen, kata dipisah satu spasi; semua tanda baca = pemisah ("PT. A & B" -> "pt a b")."""
    return " ".join(_bentuk(k) for k in _KATA.findall(teks or ""))


def _hash(garam: str, teks_normal: str) -> str:
    return hashlib.sha256(f"{garam}\x00{teks_normal}".encode("utf-8")).hexdigest()


def _bagian_bilangan(teks: str, awal: int, akhir: int) -> bool:
    """'42' in '42,000' / '1.042' / '42.5' is part of a number, not a name."""
    return bool(re.match(r"[.,]\d", teks[akhir:akhir + 2]) or re.search(r"\d[.,]$", teks[max(0, awal - 2):awal]))


@dataclass(frozen=True)
class Temuan:
    awal: int
    akhir: int
    teks: str
    jenis: str


class KamusEntitas:
    def __init__(self, garam: str, entri: dict, panjang=(), abaikan=()):
        self.garam = garam
        self.entri = dict(entri)                       # hash -> kind (e.g. "customer" or "customer,partner")
        self.abaikan = set(abaikan)                    # hash yang tidak dilaporkan pemindai/gerbang (kata umum)
        self._panjang = set(int(n) for n in panjang)   # jumlah kata nama yang ada (tanpa tahu nama mana)
        self._coba = sorted(self._panjang | set(range(1, MAKS_KATA_GABUNG + 1)), reverse=True) if self.entri else []

    # ---------- membangun ----------
    @classmethod
    def dari_nama(cls, pasangan, garam: str | None = None, abaikan=(), min_huruf: int = 2) -> "KamusEntitas":
        """pasangan: iterable of (name, kind). Empty names or names shorter than min_huruf are skipped."""
        garam = garam or secrets.token_hex(16)
        jenis_per_bentuk: dict[str, set] = {}
        for nama, jenis in pasangan:
            n = normalisasi(nama)
            gabung = n.replace(" ", "")
            if len(gabung) < min_huruf:
                continue
            jenis_per_bentuk.setdefault(n, set()).add(str(jenis))
            if " " in n and len(gabung) >= MIN_HURUF_GABUNG:
                jenis_per_bentuk.setdefault(gabung, set()).add(str(jenis))       # "WidgetWorks"
        entri = {_hash(garam, n): ",".join(sorted(j)) for n, j in jenis_per_bentuk.items()}
        panjang = {len(n.split()) for n in jenis_per_bentuk}
        k = cls(garam, entri, panjang)
        k.abaikan_nama(*abaikan)
        return k

    def abaikan_nama(self, *nama: str) -> None:
        for n in nama:
            b = normalisasi(n)
            if b:
                self.abaikan |= {_hash(self.garam, b), _hash(self.garam, b.replace(" ", ""))}

    def simpan(self, path=None) -> Path:
        path = Path(path or lokasi_bawaan()).resolve()
        repo, dasar_data = app_dir.resolve(), data_dir.resolve()
        if path.is_relative_to(repo) and not path.is_relative_to(dasar_data):
            raise ValueError("The entity dictionary file must not live inside the repo "
                             "(save it in the data folder or outside the app folder).")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"versi": VERSI, "garam": self.garam, "entri": self.entri, "panjang": sorted(self._panjang),
                "abaikan": sorted(self.abaikan)}
        sementara = path.with_suffix(path.suffix + ".tmp")
        try:
            sementara.write_text(json.dumps(data, indent=1), encoding="utf-8")
            for coba in range(5):                      # Windows: file tujuan bisa sedang dibuka pembaca
                try:
                    os.replace(sementara, path)
                    break
                except PermissionError:
                    if coba == 4:
                        raise
                    time.sleep(0.2)
        finally:
            sementara.unlink(missing_ok=True)
        return path

    @classmethod
    def muat(cls, path=None) -> "KamusEntitas":
        """File tidak ada / tidak terbaca -> OSError. Isi rusak (bukan JSON, bukan object, kunci hilang, bentuk
        salah) atau versi lain -> ValueError dengan pesan jelas (tanpa isi file)."""
        teks = Path(path or lokasi_bawaan()).read_text(encoding="utf-8")
        try:
            data = json.loads(teks)
            if not isinstance(data, dict):
                raise ValueError("bukan object")
            versi = data.get("versi")
            if versi != VERSI:
                raise ValueError(f"Versi kamus entitas {versi!r} tidak dikenal (butuh {VERSI}); bangun ulang.")
            return cls(data["garam"], data["entri"], data.get("panjang", ()), data.get("abaikan", ()))
        except ValueError as e:
            if str(e).startswith("Versi kamus entitas"):
                raise
            raise ValueError("Kamus entitas rusak (isi tidak bisa dibaca); bangun ulang.") from None
        except (KeyError, AttributeError, TypeError):
            raise ValueError("Kamus entitas rusak (bentuk isi salah); bangun ulang.") from None

    # ---------- memakai ----------
    def __len__(self) -> int:
        return len(self.entri)

    def jenis(self, nama: str) -> str | None:
        n = normalisasi(nama)
        return self.entri.get(_hash(self.garam, n)) or self.entri.get(_hash(self.garam, n.replace(" ", "")))

    def temukan(self, teks: str, termasuk_abaikan: bool = False) -> list[Temuan]:
        """Semua kemunculan nama di teks (kecocokan terpanjang dulu, tidak tumpang tindih)."""
        teks = teks or ""
        kata = list(_KATA.finditer(teks))
        bentuk = [_bentuk(k.group()) for k in kata]
        hasil, i = [], 0
        while i < len(kata):
            for n in self._coba:
                if i + n > len(kata):
                    continue
                potong = bentuk[i:i + n]
                calon = [" ".join(potong)] if n in self._panjang else []
                gabung = "".join(potong)
                if n > 1 and n <= MAKS_KATA_GABUNG and len(gabung) >= MIN_HURUF_GABUNG:
                    calon.append(gabung)
                cocok = next((h for h in (_hash(self.garam, c) for c in calon) if h in self.entri
                              and (termasuk_abaikan or h not in self.abaikan)), None)
                if not cocok:
                    continue
                awal, akhir = kata[i].start(), kata[i + n - 1].end()
                if n == 1 and potong[0].isdigit() and _bagian_bilangan(teks, awal, akhir):
                    continue
                if teks[awal:akhir].count("(") > teks[awal:akhir].count(")") and teks[akhir:akhir + 1] == ")":
                    akhir += 1                             # "Nama (Pop up)": kurung tutup ikut nama
                hasil.append(Temuan(awal, akhir, teks[awal:akhir], self.entri[cocok]))
                i += n
                break
            else:
                i += 1
        return hasil

    def samarkan(self, teks: str, label=None) -> str:
        """Ganti setiap nama dengan label. Default "[<jenis>]"; `label(temuan, urutan)` untuk pola lain.
        Nama yang sama mendapat urutan yang sama di satu teks. Nama di daftar abaikan juga disamarkan."""
        temuan = self.temukan(teks, termasuk_abaikan=True)
        urutan: dict[str, int] = {}
        keluar, posisi = [], 0
        for t in temuan:
            u = urutan.setdefault(normalisasi(t.teks).replace(" ", ""), len(urutan) + 1)
            keluar += [teks[posisi:t.awal], label(t, u) if label else f"[{t.jenis.split(',')[0]}]"]
            posisi = t.akhir
        return "".join(keluar) + teks[posisi:]


def _baca_teks(p: Path) -> str | None:
    """Isi file teks (UTF-8/UTF-16/lainnya); None kalau biner atau tidak terbaca."""
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:8192]:
        return None                                   # biner
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _siapkan_baris(baris: str) -> str:
    """Escape kode ("\\n", "\\x20", "\\u0020"), URL-encode, dan entitas HTML jadi spasi sebelum dicocokkan."""
    baris = _PERSEN.sub(" ", _ESCAPE.sub(" ", baris))
    return re.sub(r"&#?[0-9a-zA-Z]+;", " ", html.unescape(baris) if "&" in baris else baris)


def pindai_berkas(kamus: KamusEntitas, akar, pola: tuple = ("*",), lewati=None, daftar=None) -> list[tuple]:
    """Cari nama di file teks di bawah `akar`. Hasil (path relatif, baris, jenis) -- TANPA nama aslinya,
    supaya laporan pindai aman dibagikan. `daftar` = path relatif yang dipindai (mis. dari git); tanpa itu
    semua file yang cocok `pola`. `lewati(path_relatif) -> bool` untuk mengecualikan file. File biner dilewati;
    file yang tidak bisa dibaca dilaporkan sebagai (path, 0, "tidak-terbaca")."""
    akar = Path(akar)
    if daftar is None:
        daftar = sorted({f.relative_to(akar).as_posix() for pl in pola for f in akar.rglob(pl) if f.is_file()})
    hasil = []
    for rel in sorted(daftar):
        p = akar / rel
        if "__pycache__" in rel or (lewati and lewati(rel)) or not p.is_file():
            continue
        teks = _baca_teks(p)
        if teks is None:
            try:
                biner = b"\x00" in p.read_bytes()[:8192]
            except OSError:
                biner = False
            if not biner:
                hasil.append((rel, 0, "tidak-terbaca"))
            continue
        for no, baris in enumerate(teks.splitlines(), 1):
            hasil += [(rel, no, t.jenis) for t in kamus.temukan(_siapkan_baris(baris))]
    return hasil
