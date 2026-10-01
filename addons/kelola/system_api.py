"""Soft-restart API: authentication, CSRF and audit via the core management helpers; it never executes a process
command itself. The restart is requested from ``core_agent.transport.lifecycle.pengendali``: new work is held, running
work drains, and the launcher (``python app.py`` supervisor) starts a fresh worker, which applies the pending add-on
changes in ``terapkan_startup()``.

Routes (``PREFIX`` as in api.py):
  GET  {PREFIX}/system/status    {"didukung", "instance_id", "restart_id", "pekerjaan_aktif", "fase"}  manager
  POST {PREFIX}/system/restart   {"instance_id"}                                              manager + CSRF + audit
"""
import logging
import secrets

from fastapi import APIRouter, Request

from core_agent.access.context import KonteksDitolak
from core_agent.integrations.platform.config import PREFIX
from core_agent.observability import log as agent_log
from core_agent.tools import management as kelola
from core_agent.transport.lifecycle import pengendali as bawaan, GalatRestart
from .store import GalatAddon

logger = logging.getLogger("addons.kelola.system")

PATH = PREFIX + "/system"


def buat_router(store, pengendali=bawaan):
    router = APIRouter()

    @router.get(PATH + "/status", include_in_schema=False)
    def status(request: Request):
        alamat = kelola._alamat(request)
        try:
            kelola.pengelola(request)
        except kelola._Nonaktif:
            return kelola._json({"status": "nonaktif", "pesan": kelola.PESAN_NONAKTIF,
                                 "kode": secrets.token_hex(4)}, 403)
        except KonteksDitolak as e:
            return kelola._tolak(e, "system_status", alamat)
        try:
            return kelola._json({"status": "ok", **pengendali.status()})
        except Exception as e:
            kode = agent_log.catat_error_tak_terduga(logger, "[kelola sistem] status", e)
            return kelola._gagal(500, kelola.PESAN_GANGGUAN.format(kode=kode), kode)

    @router.post(PATH + "/restart", include_in_schema=False)
    async def restart(request: Request):
        def siapkan_aksi(body):
            if set(body) != {"instance_id"} or not isinstance(body.get("instance_id"), str):
                raise kelola._Galat(400, "Permintaan restart tidak valid.")

            def jalankan():
                try:
                    # Validate the pending file without applying anything to this still-running process.
                    store.terapkan_startup(periksa_saja=True)
                    return pengendali.minta_restart(body["instance_id"])
                except (GalatRestart, GalatAddon) as e:
                    raise kelola._Galat(e.status, e.pesan) from None
            return {"aksi": "system:restart", "target": body["instance_id"]}, jalankan
        return await kelola._jalankan_post(request, "system:restart", siapkan_aksi)

    return router
