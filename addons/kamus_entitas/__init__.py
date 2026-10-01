"""Entity dictionary add-on: real names stored as salted hashes (see kamus.py)."""
from .kamus import (KamusEntitas, Temuan, VERSI, lokasi_bawaan, normalisasi,  # noqa: F401
                    pindai_berkas)

ADDON_INFO = {
    "nama": "kamus_entitas",
    "judul": "Kamus Entitas",
    "deskripsi": ("Stores real names (customers, partners, ...) as salted hashes so text can be scanned and "
                  "masked without ever writing the names down."),
    "versi": "0.1",
}
