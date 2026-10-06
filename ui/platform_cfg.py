"""Pembaca config platform (/platform/config.json, kontrak platform v1) untuk dashboard.

Di bawah platform, dashboard dibuka lewat login platform: nginx menambah header X-Platform-Proxy-Token, dan token admin
gateway diisi admin di halaman Pengaturan platform. Tanpa file ini (laptop) dashboard bekerja seperti biasa.
Hanya pustaka standar.
"""

import hmac
import json
import os
from typing import Optional

ENV_PATH = "NIGATE_PLATFORM_CONFIG"
PATH_BAWAAN = "/platform/config.json"
HEADER_PROXY = "X-Platform-Proxy-Token"
URL_ADMIN_PLATFORM = "http://nigate-gateway:4001"


class ConfigRusak(Exception):
    """File config platform ada tetapi tidak bisa dipakai (rusak, atau versi skema lain)."""


def path() -> str:
    return (os.environ.get(ENV_PATH) or "").strip() or PATH_BAWAAN


def baca(lokasi: Optional[str] = None) -> Optional[dict]:
    """None = file tidak ada (bukan mode platform). File rusak / schema_version selain 1 -> ConfigRusak."""
    lokasi = lokasi or path()
    try:
        with open(lokasi, "rb") as f:
            isi = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ConfigRusak(f"Config platform {lokasi} tidak bisa dibaca: {e.strerror or e}") from None
    try:
        data = json.loads(isi)
    except ValueError:
        raise ConfigRusak(f"Config platform {lokasi} bukan JSON yang valid.") from None
    if not isinstance(data, dict):
        raise ConfigRusak(f"Config platform {lokasi} harus berupa objek JSON.")
    if data.get("schema_version") != 1:
        raise ConfigRusak(f"Config platform {lokasi}: schema_version {data.get('schema_version')!r} tidak didukung (hanya 1).")
    return data


def _teks(nilai) -> str:
    return nilai.strip() if isinstance(nilai, str) else ""


def token_admin(cfg: dict) -> str:
    return _teks((cfg.get("settings") or {}).get("NIGATE_ADMIN_TOKEN"))


def proxy_token(cfg: dict) -> str:
    return _teks((cfg.get("platform") or {}).get("proxy_token"))


def header_sah(nilai_header: Optional[str], cfg: dict) -> bool:
    """Header dari nginx platform harus sama persis dengan token di config (constant-time). Token kosong = tolak semua."""
    token = proxy_token(cfg)
    if not token or not isinstance(nilai_header, str):
        return False
    return hmac.compare_digest(nilai_header.encode(), token.encode())


def header_proxy() -> str:
    """Nilai header proxy pada request (dan handshake websocket) sesi Streamlit ini."""
    import streamlit as st

    try:
        return st.context.headers.get(HEADER_PROXY, "") or ""
    except Exception:  # di luar konteks request (mis. tes) tidak ada header
        return ""
