"""Add-on discovery and pending on/off transactions.

Add-ons are discovered, never hard-coded: every sub-package of the ``addons`` package (except ``kelola`` itself and
names starting with an underscore) is an add-on. The package is imported lazily and tolerantly; an add-on that fails to
import is listed as unavailable with a short reason and never breaks listing or startup.

Add-on contract (every item is OPTIONAL; a package without any of them is still listed and toggleable):

``ADDON_INFO``  module-level dict in the add-on package ``__init__``:
                ``{"nama": ..., "judul": ..., "deskripsi": ..., "versi": ...}``. Missing keys or a missing dict ->
                the title is derived from the folder name and the description is empty. The folder name is always
                the identity of the add-on (``nama`` in the dict is informational).
``settings``    sub-module ``addons.<nama>.settings`` with ``aktif() -> bool`` and ``set_aktif(bool)``. When both exist
                the add-on owns its own "enabled" flag and Kelola reads/writes it through them. Otherwise Kelola keeps
                the flag itself in ``state.json`` (see ``aktif()`` below for add-ons that want to read it).
``dipasang()``  callable in the package; returning False marks the add-on as unavailable in this process.
``saat_diubah(aktif)``
                callable in the package, called best-effort right after Kelola applied a toggle at startup.

Toggle semantics: a toggle never takes effect while the application runs. ``atur()`` only records the wanted value in
``<data_dir>/addons/pending.json`` (atomic write). ``terapkan_startup()`` applies and clears the pending changes once,
at the next start, before the API server is built. "aktif_sekarang" is therefore the value in effect (persisted at the
last start) and "aktif_diminta" is the value that will be in effect after the next restart.
"""
import hashlib
import importlib
import json
import logging
import pkgutil
import re
import threading
import time
from pathlib import Path

from core_agent.storage.atomic import tulis_json_atomik

logger = logging.getLogger("addons.kelola")

NAMA_KELOLA = "kelola"
NAMA_PAKET = "addons"
_POLA_NAMA = re.compile(r"[a-z][a-z0-9_]{0,63}")
_POLA_MODUL = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,80}")
_JEDA_ULANG_GAGAL = 30.0          # seconds before a failed add-on import is retried by a listing
_MAKS_JUDUL, _MAKS_DESKRIPSI, _MAKS_VERSI, _MAKS_ALASAN = 80, 300, 40, 160


class GalatAddon(Exception):
    def __init__(self, status, pesan):
        super().__init__(pesan)
        self.status, self.pesan = status, pesan


def _bersih(nilai, maks):
    """One line of text without control characters, truncated; non-strings become an empty string."""
    if not isinstance(nilai, str):
        return ""
    teks = "".join(" " if (not c.isprintable()) else c for c in nilai)
    return " ".join(teks.split())[:maks]


def _baca(path, kosong):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return kosong
    except (OSError, ValueError):
        raise GalatAddon(503, "Setelan add-on tidak terbaca. Perbaiki berkas sebelum melanjutkan.") from None
    if not isinstance(data, dict):
        raise GalatAddon(503, "Setelan add-on harus berupa objek JSON.")
    return data


def _alasan_gagal(e):
    """Short, path-free reason for a failed add-on import."""
    nama = getattr(e, "name", None)
    if isinstance(e, ModuleNotFoundError) and isinstance(nama, str) and _POLA_MODUL.fullmatch(nama):
        return f"Gagal dimuat: modul '{nama}' tidak ditemukan."
    return f"Gagal dimuat ({type(e).__name__})."


class PengelolaAddon:
    """Lists add-ons and records/applies pending toggles. ``folder`` defaults to ``<data_dir>/addons``."""

    def __init__(self, folder=None, paket=NAMA_PAKET):
        if folder is None:
            from core_agent.config import data_dir
            folder = Path(data_dir) / "addons"
        self.folder = Path(folder)
        self.paket = paket
        self.path_pending = self.folder / "pending.json"
        self.path_state = self.folder / "state.json"
        self.lock = threading.RLock()
        self._info = {}               # nama -> (waktu, hasil muat); failures expire, successes stay

    # ---------- discovery ----------
    def nama_addon(self):
        """Sorted folder names of all add-on sub-packages (nothing is imported except the parent package)."""
        try:
            induk = importlib.import_module(self.paket)
            jalur = list(getattr(induk, "__path__", []))
        except Exception:
            return []
        hasil = set()
        for m in pkgutil.iter_modules(jalur):
            if m.ispkg and m.name != NAMA_KELOLA and _POLA_NAMA.fullmatch(m.name):
                hasil.add(m.name)
        return sorted(hasil)

    def _muat(self, nama):
        """{'modul', 'judul', 'deskripsi', 'versi', 'tersedia', 'alasan'}; import is cached, never raises."""
        with self.lock:
            kini = time.monotonic()
            cache = self._info.get(nama)
            if cache is not None and (cache[1]["tersedia"] or kini - cache[0] < _JEDA_ULANG_GAGAL):
                return cache[1]
            judul = nama.replace("_", " ").title()
            hasil = {"modul": None, "judul": judul, "deskripsi": "", "versi": "", "tersedia": True, "alasan": ""}
            try:
                modul = importlib.import_module(f"{self.paket}.{nama}")
            except (Exception, SystemExit) as e:
                logger.warning("[addons] add-on %s could not be imported (%s)", nama, type(e).__name__)
                hasil.update(tersedia=False, alasan=_alasan_gagal(e))
            else:
                info = getattr(modul, "ADDON_INFO", None)
                info = info if isinstance(info, dict) else {}
                hasil.update(modul=modul,
                             judul=_bersih(info.get("judul"), _MAKS_JUDUL) or judul,
                             deskripsi=_bersih(info.get("deskripsi"), _MAKS_DESKRIPSI),
                             versi=_bersih(info.get("versi") if isinstance(info.get("versi"), str)
                                           else str(info.get("versi") or ""), _MAKS_VERSI))
                dipasang = getattr(modul, "dipasang", None)
                if callable(dipasang):
                    try:
                        if dipasang() is False:
                            hasil.update(tersedia=False, alasan="Belum terpasang di proses ini.")
                    except Exception as e:
                        hasil.update(tersedia=False, alasan=f"Pemeriksaan pemasangan gagal ({type(e).__name__}).")
            self._info[nama] = (kini, hasil)
            return hasil

    def _pengaturan(self, nama):
        """The add-on's own settings module when it offers aktif()/set_aktif(), else None."""
        try:
            modul = importlib.import_module(f"{self.paket}.{nama}.settings")
        except Exception:
            return None
        if callable(getattr(modul, "aktif", None)) and callable(getattr(modul, "set_aktif", None)):
            return modul
        return None

    # ---------- persisted state ----------
    def _state(self):
        data = _baca(self.path_state, {})
        aktif = data.get("aktif")
        return {n: v for n, v in (aktif if isinstance(aktif, dict) else {}).items()
                if isinstance(n, str) and type(v) is bool}

    def aktif(self, nama, bawaan=True):
        """Flag kept by Kelola for ``nama`` (add-ons without their own settings module). Unknown/unreadable ->
        ``bawaan``. Cheap and safe to call from an add-on at import time."""
        try:
            return self._state().get(nama, bawaan)
        except GalatAddon:
            return bawaan

    def _pending(self):
        data = _baca(self.path_pending, {})
        if not data:
            return {}
        perubahan = data.get("perubahan")
        if set(data) != {"perubahan"} or not isinstance(perubahan, dict) or any(
                not isinstance(n, str) or not _POLA_NAMA.fullmatch(n) or type(v) is not bool
                for n, v in perubahan.items()):
            raise GalatAddon(503, "Pilihan add-on tertunda tidak valid.")
        return dict(perubahan)

    def _tulis_pending(self, pending):
        try:
            tulis_json_atomik(self.path_pending, {"perubahan": pending} if pending else {})
        except OSError:
            raise GalatAddon(503, "Pilihan add-on tidak dapat disimpan; periksa penyimpanan server.") from None

    def _sekarang(self, nama, tersedia, state):
        """(value in effect or None when unknown, 'settings' | 'kelola')."""
        if not tersedia:
            return None, "kelola"
        pengaturan = self._pengaturan(nama)
        if pengaturan is not None:
            try:
                nilai = pengaturan.aktif()
            except Exception:
                return None, "settings"
            return (nilai if type(nilai) is bool else None), "settings"
        return state.get(nama, True), "kelola"

    # ---------- public operations ----------
    def daftar(self):
        with self.lock:
            pending = self._pending()
            state = self._state()
            baris = []
            for nama in self.nama_addon():
                info = self._muat(nama)
                sekarang, sumber = self._sekarang(nama, info["tersedia"], state)
                alasan = info["alasan"]
                if info["tersedia"] and sekarang is None:
                    alasan = "Status add-on tidak terbaca."
                diminta = pending.get(nama, sekarang)
                baris.append({"nama": nama, "judul": info["judul"], "deskripsi": info["deskripsi"],
                              "versi": info["versi"], "tersedia": info["tersedia"], "sumber": sumber,
                              "aktif_sekarang": sekarang, "aktif_diminta": diminta,
                              "perlu_restart": sekarang is not None and diminta != sekarang,
                              "bisa_diatur": bool(info["tersedia"] and sekarang is not None),
                              "alasan": _bersih(alasan, _MAKS_ALASAN)})
            ringkas = [[b["nama"], b["tersedia"], b["aktif_sekarang"], b["aktif_diminta"]] for b in baris]
            sidik = hashlib.sha256(json.dumps(ringkas, sort_keys=True).encode()).hexdigest()
            return {"status": "ok", "sidik": sidik, "addons": baris,
                    "tertunda": sum(1 for b in baris if b["perlu_restart"])}

    def ada(self, nama):
        return isinstance(nama, str) and nama in self.nama_addon()

    def atur(self, nama, aktif, sidik):
        """Record the wanted state of one add-on as a pending change (applied at the next start)."""
        if not isinstance(nama, str) or type(aktif) is not bool or not isinstance(sidik, str):
            raise GalatAddon(400, "Add-on atau nilai aktif tidak valid.")
        with self.lock:
            data = self.daftar()
            if sidik != data["sidik"]:
                raise GalatAddon(409, "Setelan berubah sejak ditampilkan. Segarkan lalu ulangi.")
            item = next((b for b in data["addons"] if b["nama"] == nama), None)
            if item is None:
                raise GalatAddon(404, "Add-on tidak ditemukan. Segarkan daftar.")
            if not item["bisa_diatur"]:
                raise GalatAddon(409, "Add-on ini tidak tersedia, jadi pilihannya tidak dapat diubah.")
            pending = self._pending()
            if aktif == item["aktif_sekarang"]:
                pending.pop(nama, None)
            else:
                pending[nama] = aktif
            self._tulis_pending(pending)
            return self.daftar()

    def _terapkan_satu(self, nama, aktif):
        pengaturan = self._pengaturan(nama)
        if pengaturan is not None:
            pengaturan.set_aktif(aktif)
        else:
            state = self._state()
            state[nama] = aktif
            tulis_json_atomik(self.path_state, {"aktif": state})
        modul = self._muat(nama)["modul"]
        kait = getattr(modul, "saat_diubah", None)
        if callable(kait):
            try:
                kait(aktif)
            except Exception as e:
                logger.warning("[addons] saat_diubah of %s failed (%s)", nama, type(e).__name__)

    def terapkan_startup(self, periksa_saja=False):
        """Apply and clear the pending changes. Idempotent; call once at process start, never from an endpoint or a
        UI rerun. ``periksa_saja=True`` only validates the pending file and reports whether changes are waiting.
        A changed item that fails to apply stays pending for the next start; unknown add-ons are dropped."""
        with self.lock:
            if not self.path_pending.exists():
                return False
            try:
                pending = self._pending()
            except GalatAddon as e:
                if periksa_saja:
                    raise
                logger.warning("[addons] pending changes ignored: %s", e.pesan)
                return False
            if not pending:
                return False
            if periksa_saja:
                return True
            dikenal = set(self.nama_addon())
            sisa = {}
            for nama, aktif in pending.items():
                if nama not in dikenal:
                    continue
                try:
                    self._terapkan_satu(nama, aktif)
                except Exception as e:
                    logger.warning("[addons] applying %s failed (%s); kept for the next start", nama,
                                   type(e).__name__)
                    sisa[nama] = aktif
            try:
                self._tulis_pending(sisa)
            except GalatAddon as e:
                logger.warning("[addons] pending file not cleared: %s", e.pesan)
            return True
