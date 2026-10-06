"""Tes mode platform dashboard: dibuka lewat login platform (nginx menambah X-Platform-Proxy-Token), token admin dari
Pengaturan platform. Jalankan:  python -m unittest discover -s ui -p "tes_*.py" -v"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import klien  # noqa: E402
import platform_cfg  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402
from tes_app import APP, KlienPalsu  # noqa: E402

TOKEN_PROXY = "proksi-" + "p" * 40
TOKEN_ADMIN = "a" * 64


def tulis(folder, settings=None, proxy_token=TOKEN_PROXY, schema=1):
    p = os.path.join(folder, "config.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"schema_version": schema, "team": "nigate", "settings": settings if settings is not None else {"NIGATE_ADMIN_TOKEN": TOKEN_ADMIN},
                   "platform": {"proxy_token": proxy_token}}, f)
    return p


class TesBacaConfig(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_file_tidak_ada_berarti_bukan_mode_platform(self):
        self.assertIsNone(platform_cfg.baca(os.path.join(self.d, "config.json")))

    def test_file_rusak_atau_skema_lain_adalah_galat_bukan_kosong(self):
        p = os.path.join(self.d, "config.json")
        for isi in ['{"schema_version": 1, "settings": ', '{"schema_version": 2}', '{"settings": {}}', "[]"]:
            with open(p, "w", encoding="utf-8") as f:
                f.write(isi)
            with self.assertRaises(platform_cfg.ConfigRusak, msg=isi):
                platform_cfg.baca(p)

    def test_token_admin_dan_token_proxy_dibaca(self):
        c = platform_cfg.baca(tulis(self.d, {"NIGATE_ADMIN_TOKEN": "  " + TOKEN_ADMIN + " "}))
        self.assertEqual(platform_cfg.token_admin(c), TOKEN_ADMIN)
        self.assertEqual(platform_cfg.proxy_token(c), TOKEN_PROXY)

    def test_header_proxy_dibandingkan_persis(self):
        c = platform_cfg.baca(tulis(self.d))
        self.assertTrue(platform_cfg.header_sah(TOKEN_PROXY, c))
        for salah in ["", None, TOKEN_PROXY + "x", TOKEN_PROXY[:-1], TOKEN_PROXY.upper()]:
            self.assertFalse(platform_cfg.header_sah(salah, c), salah)

    def test_token_proxy_kosong_menolak_semua(self):
        c = platform_cfg.baca(tulis(self.d, proxy_token=""))
        self.assertFalse(platform_cfg.header_sah("", c))
        self.assertFalse(platform_cfg.header_sah("apa-saja", c))


class TesModePlatform(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        KlienPalsu.panggilan = []
        KlienPalsu.kosong = False
        self.dibuat = []

    def jalankan(self, header, path=None, env=None):
        dibuat = self.dibuat

        class Catat(KlienPalsu):
            def __init__(self, base_url, token, timeout=10.0):
                super().__init__(base_url, token, timeout)
                dibuat.append((base_url, token))

        at = AppTest.from_file(APP, default_timeout=30)
        lingkungan = {"NIGATE_PLATFORM_CONFIG": path or os.path.join(self.d, "config.json"), **(env or {})}
        with mock.patch.dict(os.environ, lingkungan, clear=False), mock.patch.object(klien, "KlienAdmin", Catat), \
                mock.patch.object(platform_cfg, "header_proxy", return_value=header):
            for nama in ("NIGATE_ADMIN_TOKEN", "NIGATE_ADMIN_URL"):
                if nama not in (env or {}):
                    os.environ.pop(nama, None)
            at.run()
        self.assertFalse(at.exception)
        return at

    def test_lewat_platform_terhubung_tanpa_mengetik_token(self):
        tulis(self.d)
        at = self.jalankan(TOKEN_PROXY)
        self.assertEqual(self.dibuat[0], ("http://nigate-gateway:4001", TOKEN_ADMIN))
        self.assertEqual(len(at.tabs), 5)
        self.assertEqual(len(at.sidebar.text_input), 0, "alamat & token tidak diketik di mode platform")

    def test_alamat_admin_bisa_diganti_lewat_env(self):
        tulis(self.d)
        self.jalankan(TOKEN_PROXY, env={"NIGATE_ADMIN_URL": "http://lain:4001"})
        self.assertEqual(self.dibuat[0][0], "http://lain:4001")

    def test_tanpa_header_proxy_yang_benar_ditolak_dan_gateway_tidak_dipanggil(self):
        tulis(self.d)
        for header in ["", "salah"]:
            self.dibuat.clear()
            at = self.jalankan(header)
            self.assertEqual(self.dibuat, [])
            self.assertEqual(len(at.tabs), 0)
            self.assertTrue(any("ditolak" in e.value.lower() for e in at.error), header)

    def test_token_proxy_belum_ada_menampilkan_langkah_setup(self):
        tulis(self.d, proxy_token="")
        at = self.jalankan("")
        self.assertEqual(self.dibuat, [])
        self.assertTrue(any("platform" in e.value.lower() for e in at.error))

    def test_token_admin_belum_diisi_di_platform(self):
        tulis(self.d, settings={})
        at = self.jalankan(TOKEN_PROXY)
        self.assertEqual(self.dibuat, [])
        self.assertTrue(any("NIGATE_ADMIN_TOKEN" in i.value for i in at.info))

    def test_config_platform_rusak_menampilkan_galat_jelas(self):
        p = os.path.join(self.d, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{rusak")
        at = self.jalankan(TOKEN_PROXY)
        self.assertEqual(self.dibuat, [])
        self.assertTrue(any("config platform" in e.value.lower() for e in at.error))


if __name__ == "__main__":
    unittest.main()
