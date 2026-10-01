"""On/off switch and limits of the advisor add-on. Default: OFF.

Stored as JSON at ``<core_agent.config.data_dir>/addons/konsultasi_pakar.json``, e.g.
``{"aktif": true, "maks_konsultasi": 2}``. Only ``aktif`` matters for switching; a missing file or missing key
means the default (``aktif`` False, ``maks_konsultasi`` 2). Unknown keys or wrong types raise ValueError (the
advisor treats that as "unavailable"). Writes are atomic.

The module offers ``aktif()`` and ``set_aktif(bool)``, which is the settings contract the add-on manager
(``addons.kelola``) uses to read and change the flag. The `config_path` parameters are kept only for call
compatibility with older callers; the location always follows the core data dir (env NIRINA_DATA_DIR).
"""
import json
import os
import secrets
from pathlib import Path

BAWAAN = {'aktif': False, 'maks_konsultasi': 2}
MAKS_TERTINGGI = 5


def lokasi(config_path=None):
    from core_agent.config import data_dir   # imported lazily: the core creates folders on import
    return Path(data_dir) / 'addons' / 'konsultasi_pakar.json'


def baca(config_path=None):
    """Return {'aktif': bool, 'maks_konsultasi': int}; defaults when the file is absent."""
    try:
        data = json.loads(lokasi(config_path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return dict(BAWAAN)
    if not isinstance(data, dict) or not set(data) <= set(BAWAAN):
        raise ValueError('Invalid advisor add-on settings')
    hasil = {**BAWAAN, **data}
    maks = hasil['maks_konsultasi']
    if type(hasil['aktif']) is not bool or type(maks) is not int or not 0 <= maks <= MAKS_TERTINGGI:
        raise ValueError('Invalid advisor add-on settings')
    return hasil


def aktif(config_path=None):
    return baca(config_path)['aktif']


def set_aktif(nilai):
    """Switch the add-on on/off (keeps the consultation limit); used by the add-on manager."""
    if type(nilai) is not bool:
        raise ValueError('aktif must be a bool')
    return tulis(aktif=nilai)['aktif']


def _tulis_atomik(path, data):
    try:
        from core_agent.storage.atomic import tulis_json_atomik
    except ImportError:   # minimal fallback when the core helper is absent
        path.parent.mkdir(parents=True, exist_ok=True)
        sementara = path.with_name(f'.{path.name}.{secrets.token_hex(6)}.tmp')
        try:
            sementara.write_text(json.dumps(data, indent=1, sort_keys=True) + '\n', encoding='utf-8')
            os.replace(sementara, path)
        finally:
            if sementara.exists():
                sementara.unlink()
    else:
        tulis_json_atomik(path, data)


def tulis(aktif=None, maks_konsultasi=None, config_path=None):
    """Update the given values (others kept), validate and write atomically; returns the new settings."""
    baru = baca(config_path)
    if aktif is not None:
        baru['aktif'] = aktif
    if maks_konsultasi is not None:
        baru['maks_konsultasi'] = maks_konsultasi
    if (type(baru['aktif']) is not bool or type(baru['maks_konsultasi']) is not int
            or not 0 <= baru['maks_konsultasi'] <= MAKS_TERTINGGI):
        raise ValueError('Invalid advisor add-on settings')
    _tulis_atomik(lokasi(config_path), baru)
    return baru
