"""Add-on API: thin adapter onto the core management auth, CSRF and audit helpers.

Routes (``PREFIX`` = ``core_agent.integrations.platform.config.PREFIX``, env NIRINA_ADMIN_PREFIX, default /admin):
  GET  {PREFIX}/addons        list of discovered add-ons          manager credential
  POST {PREFIX}/addons/atur   {"nama", "aktif", "sidik"}          manager credential + CSRF + audit

Authentication, CSRF (token from GET {PREFIX}/tools/csrf) and the audit log are exactly those of the tool
management routes in ``core_agent.tools.management``; nothing is re-implemented here.
"""
import logging
import secrets

from fastapi import APIRouter, Request

from core_agent.access.context import KonteksDitolak
from core_agent.integrations.platform.config import PREFIX
from core_agent.observability import log as agent_log
from core_agent.tools import management as kelola
from .store import GalatAddon

logger = logging.getLogger("addons.kelola.api")

PATH = PREFIX + "/addons"


def buat_router(store):
    router = APIRouter()

    @router.get(PATH, include_in_schema=False)
    def daftar(request: Request):
        alamat = kelola._alamat(request)
        try:
            kelola.pengelola(request)
        except kelola._Nonaktif:
            return kelola._json({"status": "nonaktif", "pesan": kelola.PESAN_NONAKTIF,
                                 "kode": secrets.token_hex(4)}, 403)
        except KonteksDitolak as e:
            return kelola._tolak(e, "kelola_addon_daftar", alamat)
        try:
            return kelola._json({**store.daftar(), "mode_kelola": kelola.mode_kelola()})
        except GalatAddon as e:
            return kelola._gagal(e.status, e.pesan)
        except Exception as e:
            kode = agent_log.catat_error_tak_terduga(logger, "[kelola addon] daftar", e)
            return kelola._gagal(500, kelola.PESAN_GANGGUAN.format(kode=kode), kode)

    @router.post(PATH + "/atur", include_in_schema=False)
    async def atur(request: Request):
        def siapkan_aksi(body):
            if set(body) != {"nama", "aktif", "sidik"} or not isinstance(body.get("nama"), str) \
                    or type(body.get("aktif")) is not bool or not isinstance(body.get("sidik"), str):
                raise kelola._Galat(400, "Permintaan pengaturan add-on tidak valid.")
            if not store.ada(body["nama"]):
                raise kelola._Galat(404, "Add-on tidak ditemukan. Segarkan daftar.")

            def jalankan():
                try:
                    return store.atur(body["nama"], body["aktif"], body["sidik"])
                except GalatAddon as e:
                    raise kelola._Galat(e.status, e.pesan) from None
            return {"aksi": "addon:atur", "target": body["nama"],
                    "sesudah": {"aktif_diminta": body["aktif"]}}, jalankan
        return await kelola._jalankan_post(request, "addon:atur", siapkan_aksi)

    return router
