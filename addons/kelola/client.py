"""Streamlit-side HTTP client for the Kelola routes.

Reuses the headers, timeouts, connection handling and result shaping of ``core_agent.transport.client`` (the same
helpers the tool management tab uses). Every function returns a dict whose ``status`` is "ok" on success; any other
status carries a safe ``pesan`` for the user.

Credentials: reads send the server key from the environment (if any) and the account header hook, or the key typed by
the operator (``kunci``). Writes (CSRF fetch and POST) never use the environment key: only a key typed by the operator
or an account credential from the header hook.
"""
from core_agent.integrations.platform.config import PREFIX
from core_agent.transport import client as api

PATH_ADDONS = PREFIX + "/addons"
PATH_SISTEM = PREFIX + "/system"


def _panggil(kanal, path, body=None, csrf=None, kunci=None):
    try:
        ubah = body is not None
        header = api._header_kelola(ubah, kunci)
        if not ubah and kunci and kunci.strip():
            header[api.HEADER_KELOLA] = kunci.strip()       # read with the key typed by the operator
        kw = {"headers": header, "timeout": api._batas(api.BATAS_KELOLA)}
        if ubah:
            header[api.HEADER_CSRF] = csrf
            kw["json"] = body
        resp = api._kirim(kanal, api.requests.post if ubah else api.requests.get,
                          f"{api.AGENT_API_BASE_URL}{path}", **kw)
        if resp is None:
            return api._hasil_putus()
        data = api._json_atau_none(resp)
        if resp.status_code >= 400:
            return api._hasil_kelola(resp, data)
        if not isinstance(data, dict):
            return {"status": "error", "pesan": "Respons server tidak valid."}
        return {**data, "status": "ok"}
    except api.requests.exceptions.RequestException as e:
        return api._hasil_koneksi_gagal(e, kanal)
    except Exception as e:
        return api._hasil_bug(e, kanal)


def daftar(kunci=None):
    """GET {PREFIX}/addons -> {status:'ok', sidik, addons:[...], tertunda, mode_kelola}."""
    return _panggil("kelola addon daftar", PATH_ADDONS, kunci=kunci)


def atur(nama, aktif, sidik, kunci=None):
    """POST {PREFIX}/addons/atur. Retried once, and only when the server explicitly rejected the CSRF token (before
    any change was made)."""
    kanal = "kelola addon atur"
    for percobaan in range(2):
        token = api.kelola_tool_csrf(kunci)
        if token.get("status") != "ok" or not token.get("csrf"):
            return token
        hasil = _panggil(kanal, PATH_ADDONS + "/atur", {"nama": nama, "aktif": aktif, "sidik": sidik},
                         token["csrf"], kunci)
        if percobaan == 0 and hasil.get("http") == 403 and hasil.get("pesan") == api._PESAN_CSRF_SERVER:
            continue
        return hasil
    return {"status": "ditolak", "pesan": api.PESAN_AKSES_DITOLAK}


def status_sistem(kunci=None):
    """GET {PREFIX}/system/status -> {status:'ok', didukung, instance_id, restart_id, pekerjaan_aktif, fase}."""
    return _panggil("kelola sistem status", PATH_SISTEM + "/status", kunci=kunci)


def restart_sistem(instance_id, kunci=None):
    """POST {PREFIX}/system/restart. Not retried: a timeout can happen after the server accepted the request."""
    token = api.kelola_tool_csrf(kunci)
    if token.get("status") != "ok" or not token.get("csrf"):
        return token
    return _panggil("kelola sistem restart", PATH_SISTEM + "/restart", {"instance_id": instance_id},
                    token["csrf"], kunci)
