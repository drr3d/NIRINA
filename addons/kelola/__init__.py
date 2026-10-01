"""Kelola: generic add-on management (list, pending on/off toggles, soft restart).

Light on import (the Streamlit process imports ``addons.kelola.client`` through this package): the HTTP routes and the
core management module are only loaded by ``pasang()``.

What a host has to do: nothing beyond the call the core launcher already makes before it starts the API server,

    from addons.kelola import terapkan_startup
    terapkan_startup()

``terapkan_startup()`` (1) applies pending add-on toggles (see store.py for the add-on contract) and (2) calls
``pasang()``, which mounts the routes ``{PREFIX}/addons`` and ``{PREFIX}/system`` on the core management router
(``core_agent.tools.management.router``). The API server includes that router when ``core_agent.transport.server`` is
imported, which happens after ``terapkan_startup()``. If the server module was imported earlier, ``pasang()`` also
includes the routes directly on ``server.app``. A host that builds its own FastAPI app can call
``pasang(app_or_router)`` explicitly. Every call is idempotent and the routes use the same credential, CSRF and audit
rules as the other management routes (``NIRINA_KELOLA_KUNCI`` or a context policy).
"""
import logging
import sys

from .store import PengelolaAddon, GalatAddon

logger = logging.getLogger("addons.kelola")

_store = None
_terpasang = []          # targets (app or router) that already carry the routes


def pengelola():
    """The process-wide PengelolaAddon (state under <data_dir>/addons)."""
    global _store
    if _store is None:
        _store = PengelolaAddon()
    return _store


def aktif(nama, bawaan=True):
    """Kelola's own enabled flag for add-on ``nama`` (used by add-ons that have no settings module)."""
    return pengelola().aktif(nama, bawaan)


def pasang(app_or_router=None):
    """Mount the Kelola routes. ``None`` -> the core management router, plus ``server.app`` when the server module is
    already imported; otherwise the given FastAPI app or APIRouter. Idempotent per target; returns the number of
    targets mounted by this call."""
    from core_agent.tools import management
    from core_agent.transport import lifecycle
    from .api import buat_router, PATH as PATH_ADDONS
    from .system_api import buat_router as buat_router_sistem, PATH as PATH_SISTEM
    if app_or_router is not None:
        sasaran = [app_or_router]
    else:
        sasaran = [management.router]
        server = sys.modules.get("core_agent.transport.server")
        aplikasi = getattr(server, "app", None)
        if aplikasi is not None:
            sasaran.append(aplikasi)
    baru = 0
    for target in sasaran:
        if any(target is t for t in _terpasang):
            continue
        target.include_router(buat_router(pengelola()))
        target.include_router(buat_router_sistem(pengelola()))
        _terpasang.append(target)
        baru += 1
    # Keep these endpoints served while a restart drains, and not counted as active work.
    lifecycle.daftarkan_kontrol("GET", PATH_ADDONS)
    lifecycle.daftarkan_kontrol("GET", PATH_SISTEM + "/status")
    lifecycle.daftarkan_kontrol("POST", PATH_SISTEM + "/restart")
    return baru


def terapkan_startup():
    """Apply pending toggles, then mount the routes. Called once by the launcher before the API server starts.
    Returns True when pending changes were applied. A failing mount is logged, never raised."""
    diterapkan = pengelola().terapkan_startup()
    try:
        pasang()
    except Exception as e:
        logger.warning("[addons] Kelola routes not mounted (%s)", type(e).__name__)
    return diterapkan
