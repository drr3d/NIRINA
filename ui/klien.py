"""Klien HTTP untuk API admin nigate. Hanya memakai pustaka standar (tanpa dependency tambahan)."""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


class GalatGateway(Exception):
    """Dasar semua galat dari klien admin."""


class TidakTerjangkau(GalatGateway):
    """Gateway tidak menjawab (mati, alamat salah, atau timeout)."""


class TokenDitolak(GalatGateway):
    """Token admin salah atau tidak diberikan."""


class GalatAdmin(GalatGateway):
    """API admin menjawab dengan galat (mis. validasi, key tidak ditemukan)."""

    def __init__(self, status: int, kode: str, pesan: str):
        super().__init__(pesan)
        self.status, self.kode, self.pesan = status, kode, pesan


# Penanda "field tidak dikirim" (berbeda dari None yang berarti null = hapus batas).
TIDAK_DIKIRIM: Any = object()


class KlienAdmin:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Alamat admin harus diawali http:// atau https://")
        self.base_url, self._token, self.timeout = base_url, token or "", timeout

    def _panggil(self, metode: str, jalur: str, body: Optional[dict] = None, query: Optional[dict] = None) -> dict:
        url = self.base_url + jalur
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=metode)
        req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            info = _baca_galat(e)
            if e.code == 401:
                raise TokenDitolak("Token admin ditolak oleh gateway.") from None
            raise GalatAdmin(e.code, info.get("code", "unknown"), info.get("message", f"HTTP {e.code}")) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            alasan = getattr(e, "reason", e)
            raise TidakTerjangkau(f"Gateway tidak dapat dihubungi di {self.base_url} ({alasan}).") from None

    # ---- endpoint ----
    def health(self) -> dict:
        return self._panggil("GET", "/admin/health")

    def config(self) -> dict:
        return self._panggil("GET", "/admin/config")

    def keys(self) -> list:
        return self._panggil("GET", "/admin/keys")["keys"]

    def buat_key(self, name: str, rpm: Optional[int] = None, tpm: Optional[int] = None, metadata: Optional[dict] = None) -> dict:
        body = {"name": name, "rpm": rpm, "tpm": tpm}
        if metadata:
            body["metadata"] = metadata
        return self._panggil("POST", "/admin/keys", body)

    def ubah_key(
        self, name: str, active: Any = TIDAK_DIKIRIM, rpm: Any = TIDAK_DIKIRIM, tpm: Any = TIDAK_DIKIRIM, metadata: Any = TIDAK_DIKIRIM,
        user_rpm: Any = TIDAK_DIKIRIM, user_tpm: Any = TIDAK_DIKIRIM, user_required: Any = TIDAK_DIKIRIM,
    ) -> dict:
        """metadata: dict mengganti seluruh isi, None mengosongkan, TIDAK_DIKIRIM membiarkan. user_rpm/user_tpm: batas
        bawaan per client (None = tanpa batas per client)."""
        pasangan = (("active", active), ("rpm", rpm), ("tpm", tpm), ("metadata", metadata),
                    ("user_rpm", user_rpm), ("user_tpm", user_tpm), ("user_required", user_required))
        body = {k: v for k, v in pasangan if v is not TIDAK_DIKIRIM}
        return self._panggil("PATCH", "/admin/keys/" + urllib.parse.quote(name, safe=""), body)

    def hapus_key(self, name: str) -> dict:
        return self._panggil("DELETE", "/admin/keys/" + urllib.parse.quote(name, safe=""))

    # ---- client (label `user` request) di bawah key ----
    def clients(self, name: str) -> dict:
        return self._panggil("GET", "/admin/keys/" + urllib.parse.quote(name, safe="") + "/users")

    def simpan_client(self, name: str, user: str, rpm: Optional[int] = None, tpm: Optional[int] = None, active: bool = True) -> dict:
        jalur = "/admin/keys/" + urllib.parse.quote(name, safe="") + "/users/" + urllib.parse.quote(user, safe="")
        return self._panggil("PUT", jalur, {"rpm": rpm, "tpm": tpm, "active": active})

    def hapus_client(self, name: str, user: str) -> dict:
        return self._panggil("DELETE", "/admin/keys/" + urllib.parse.quote(name, safe="") + "/users/" + urllib.parse.quote(user, safe=""))

    def upstreams(self) -> list:
        return self._panggil("GET", "/admin/upstreams")["upstreams"]

    def stats(self, jam: int = 24, per: str = "semua") -> dict:
        return self._panggil("GET", "/admin/stats", query={"jam": jam, "per": per})

    def kejadian_guardrail(self, jam: int = 24, limit: int = 100) -> list:
        return self._panggil("GET", "/admin/guardrail/events", query={"jam": jam, "limit": limit})["kejadian"]

    def reload(self) -> dict:
        return self._panggil("POST", "/admin/reload")


def _baca_galat(e: urllib.error.HTTPError) -> dict:
    try:
        return json.loads(e.read() or b"{}").get("error", {}) or {}
    except (ValueError, AttributeError):
        return {}
