"""Tes klien API admin memakai server HTTP palsu. Jalankan:  python -m unittest discover -s ui -p "tes_*.py" -v"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import klien  # noqa: E402
from klien import GalatAdmin, KlienAdmin, TidakTerjangkau, TokenDitolak  # noqa: E402

TOKEN = "token-uji-yang-cukup-panjang-123456"


class ServerPalsu:
    """Merekam request dan menjawab sesuai `jawaban[(metode, jalur)]` = (status, dict)."""

    def __init__(self):
        self.jawaban, self.diterima = {}, []
        parent = self

        class H(BaseHTTPRequestHandler):
            def _tangani(self):
                n = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(n)) if n else None
                jalur = self.path.split("?")[0]
                parent.diterima.append({"metode": self.command, "path": self.path, "auth": self.headers.get("authorization"), "body": body})
                if self.headers.get("authorization") != f"Bearer {TOKEN}":
                    status, obj = 401, {"error": {"code": "invalid_admin_token", "message": "Token admin tidak valid."}}
                else:
                    status, obj = parent.jawaban.get((self.command, jalur), (404, {"error": {"code": "not_found", "message": "tidak ada"}}))
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_DELETE = _tangani

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tutup(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TesKlien(unittest.TestCase):
    def setUp(self):
        self.s = ServerPalsu()
        self.addCleanup(self.s.tutup)
        self.c = KlienAdmin(self.s.url, TOKEN)

    def test_health_mengirim_bearer_token(self):
        self.s.jawaban[("GET", "/admin/health")] = (200, {"status": "ok"})
        self.assertEqual(self.c.health()["status"], "ok")
        self.assertEqual(self.s.diterima[0]["auth"], f"Bearer {TOKEN}")

    def test_token_salah_menjadi_token_ditolak(self):
        with self.assertRaises(TokenDitolak):
            KlienAdmin(self.s.url, "token-salah").health()

    def test_galat_api_membawa_status_kode_dan_pesan(self):
        self.s.jawaban[("POST", "/admin/keys")] = (409, {"error": {"code": "name_taken", "message": "Nama key 'a' sudah dipakai."}})
        with self.assertRaises(GalatAdmin) as cm:
            self.c.buat_key("a")
        self.assertEqual((cm.exception.status, cm.exception.kode), (409, "name_taken"))
        self.assertIn("sudah dipakai", cm.exception.pesan)

    def test_gateway_mati_menjadi_tidak_terjangkau_tanpa_membocorkan_token(self):
        s = ServerPalsu()
        url = s.url
        s.tutup()
        with self.assertRaises(TidakTerjangkau) as cm:
            KlienAdmin(url, TOKEN, timeout=2).health()
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertIn(url, str(cm.exception))

    def test_ubah_key_hanya_mengirim_field_yang_diberikan_dan_none_menjadi_null(self):
        self.s.jawaban[("PATCH", "/admin/keys/tim-a")] = (200, {"info": {}})
        self.c.ubah_key("tim-a", active=False)
        self.c.ubah_key("tim-a", rpm=None)
        self.c.ubah_key("tim-a", rpm=5, tpm=None)
        self.assertEqual([r["body"] for r in self.s.diterima], [{"active": False}, {"rpm": None}, {"rpm": 5, "tpm": None}])

    def test_nama_key_di_path_di_encode(self):
        self.s.jawaban[("DELETE", "/admin/keys/a%2Fb")] = (200, {"dihapus": "a/b"})
        self.assertEqual(self.c.hapus_key("a/b")["dihapus"], "a/b")

    def test_buat_key_mengirim_null_untuk_batas_kosong(self):
        self.s.jawaban[("POST", "/admin/keys")] = (201, {"key": "ngk_x", "info": {}})
        self.c.buat_key("baru", None, 30)
        self.assertEqual(self.s.diterima[0]["body"], {"name": "baru", "rpm": None, "tpm": 30})

    def test_query_stats_dan_kejadian(self):
        self.s.jawaban[("GET", "/admin/stats")] = (200, {"baris": []})
        self.s.jawaban[("GET", "/admin/guardrail/events")] = (200, {"kejadian": [1]})
        self.c.stats(6, "key")
        self.assertEqual(self.c.kejadian_guardrail(12, 50), [1])
        self.assertIn("jam=6", self.s.diterima[0]["path"])
        self.assertIn("per=key", self.s.diterima[0]["path"])
        self.assertIn("limit=50", self.s.diterima[1]["path"])

    def test_alamat_tidak_valid_ditolak(self):
        for salah in ["", "127.0.0.1:4001", "ftp://x"]:
            with self.assertRaises(ValueError):
                KlienAdmin(salah, TOKEN)

    def test_garis_miring_di_akhir_alamat_dibuang(self):
        self.s.jawaban[("GET", "/admin/health")] = (200, {"status": "ok"})
        self.assertEqual(KlienAdmin(self.s.url + "/", TOKEN).health()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
