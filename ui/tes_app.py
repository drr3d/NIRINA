"""Tes aplikasi Streamlit (AppTest) dengan klien palsu. Jalankan:  python -m unittest discover -s ui -p "tes_*.py" -v"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import klien  # noqa: E402
from klien import GalatAdmin, TidakTerjangkau, TokenDitolak  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")


def _baris(kelompok, request=100, ok=90, klien_=2, limit=3, guardrail=1, upstream=4, gateway=0, temuan=5, tm=1000, tk=2000, rata=120.0, maks=900):
    return {"kelompok": kelompok, "request": request, "ok": ok, "klien": klien_, "limit": limit, "guardrail": guardrail, "upstream": upstream,
            "gateway": gateway, "temuan": temuan, "token_masuk": tm, "token_keluar": tk, "latensi_rata_ms": rata, "latensi_maks_ms": maks}


class KlienPalsu:
    """Menggantikan klien.KlienAdmin: data tetap, dan mencatat pemanggilan yang mengubah data."""

    panggilan = []
    kosong = False

    def __init__(self, base_url, token, timeout=10.0):
        self.base_url, self.token = base_url, token

    def health(self):
        return {"status": "ok", "versi": "0.1.0", "uptime_detik": 3700, "auth_required": True, "jumlah_model": 2, "stats_aktif": True,
                "statistik_dibuang": 0, "guardrail_aktif": True, "reload_tersedia": True}

    def config(self):
        return {"limits": {"default_rpm": 120, "default_tpm": None},
                "guardrail": {"enabled": True, "mode": "redact", "scan_request": True, "scan_response": True, "entropy": True, "entropy_min_length": 32,
                              "entropy_threshold": 4.5, "aksi": {"private_key": "block"}, "rule_kustom": []},
                "models": []}

    def stats(self, jam=24, per="semua"):
        if self.kosong:
            return {"baris": []}
        if per == "semua":
            return {"baris": [_baris("semua")]}
        if per == "jam":
            sekarang = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            return {"baris": [_baris((sekarang - timedelta(hours=h)).strftime("%Y-%m-%d %H:00"), request=20 + h) for h in (2, 1, 0)]}
        return {"baris": [_baris("nirina-prod", 70), _baris("nirina-dev", 30)]}

    def keys(self):
        return [
            {"id": 1, "name": "nirina-prod", "prefix": "ngk_ab12", "active": True, "created_at": 1790000000, "rpm": None, "tpm": None, "rpm_efektif": 120, "tpm_efektif": None},
            {"id": 2, "name": "nirina-dev", "prefix": "ngk_cd34", "active": False, "created_at": 1790000100, "rpm": 5, "tpm": 900, "rpm_efektif": 5, "tpm_efektif": 900},
        ]

    def buat_key(self, name, rpm=None, tpm=None):
        KlienPalsu.panggilan.append(("buat", name, rpm, tpm))
        return {"key": "ngk_" + "f" * 64, "info": {}}

    def ubah_key(self, name, active=None, rpm=None, tpm=None):
        KlienPalsu.panggilan.append(("ubah", name, active, rpm, tpm))
        return {"info": {}}

    def hapus_key(self, name):
        KlienPalsu.panggilan.append(("hapus", name))
        return {"dihapus": name}

    def upstreams(self):
        return [
            {"alias": "nirina-main", "urutan": 1, "name": "cerebras", "model": "qwen", "url": "https://api.cerebras.ai/v1", "key_env": "CEREBRAS_API_KEY",
             "terkonfigurasi": True, "timeout_detik": 120, "gagal_beruntun": 2, "dalam_cooldown": True, "sisa_cooldown_detik": 25},
            {"alias": "nirina-main", "urutan": 2, "name": "ollama", "model": "qwen3.5", "url": "http://host/v1", "key_env": None,
             "terkonfigurasi": True, "timeout_detik": 300, "gagal_beruntun": 0, "dalam_cooldown": False, "sisa_cooldown_detik": None},
            {"alias": "nirina-cepat", "urutan": 1, "name": "groq", "model": "llama", "url": "https://api.groq.com/v1", "key_env": "GROQ_API_KEY",
             "terkonfigurasi": False, "timeout_detik": 120, "gagal_beruntun": 0, "dalam_cooldown": False, "sisa_cooldown_detik": None},
        ]

    def kejadian_guardrail(self, jam=24, limit=100):
        if self.kosong:
            return []
        return [{"ts_ms": 1790000000000, "key_name": "nirina-prod", "alias": "nirina-main", "status": 200, "hasil": "ok", "temuan_masuk": 2,
                 "temuan_keluar": 0, "jenis_temuan": "github_token,jwt"}]

    def reload(self):
        KlienPalsu.panggilan.append(("reload",))
        return {"status": "ok", "jumlah_model": 2, "perlu_restart": ["server.listen"]}


def app_baru(env=None, klien_cls=KlienPalsu):
    KlienPalsu.panggilan = []
    KlienPalsu.kosong = False
    at = AppTest.from_file(APP, default_timeout=30)
    return at, mock.patch.dict(os.environ, {"NIGATE_ADMIN_TOKEN": "token-uji", **(env or {})}, clear=False), mock.patch.object(klien, "KlienAdmin", klien_cls)


def jalankan(at, p_env, p_klien):
    with p_env, p_klien:
        at.run()
    return at


class TesApp(unittest.TestCase):
    def test_halaman_terisi_tanpa_exception_dan_punya_lima_tab(self):
        at = jalankan(*app_baru())
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual([t.label for t in at.tabs], ["Ringkasan", "Key & Limit", "Upstream", "Guardrail", "Konfigurasi"])

    def test_ringkasan_menampilkan_angka_yang_benar(self):
        at = jalankan(*app_baru())
        metrik = {m.label: m.value for m in at.metric}
        self.assertEqual(metrik["Total request"], "100")
        self.assertEqual(metrik["Berhasil"], "90,0%")
        self.assertEqual(metrik["Dibatasi (rate limit)"], "3")
        self.assertEqual(metrik["Token masuk / keluar"], "1.000 / 2.000")
        self.assertEqual(metrik["Latensi rata-rata"], "120 ms")

    def test_tanpa_token_meminta_token_dan_tidak_memanggil_gateway(self):
        at, _, p_klien = app_baru()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NIGATE_ADMIN_TOKEN", None)
            with p_klien:
                at.run()
        self.assertTrue(any("token admin" in i.value.lower() for i in at.info))
        self.assertEqual(len(at.tabs), 0)

    def test_gateway_mati_menampilkan_pesan_jelas_bukan_traceback(self):
        class Mati(KlienPalsu):
            def health(self):
                raise TidakTerjangkau("Gateway tidak dapat dihubungi di http://x (refused).")

        at = jalankan(*app_baru(klien_cls=Mati))
        self.assertFalse(at.exception)
        self.assertTrue(any("tidak dapat dihubungi" in e.value for e in at.error))
        self.assertEqual(len(at.tabs), 0)

    def test_token_salah_menampilkan_galat(self):
        class Tolak(KlienPalsu):
            def health(self):
                raise TokenDitolak("x")

        at = jalankan(*app_baru(klien_cls=Tolak))
        self.assertTrue(any("ditolak" in e.value for e in at.error))

    def test_galat_di_satu_tab_tidak_merusak_tab_lain(self):
        class Rusak(KlienPalsu):
            def upstreams(self):
                raise GalatAdmin(500, "internal", "Galat internal.")

        at = jalankan(*app_baru(klien_cls=Rusak))
        self.assertFalse(at.exception)
        self.assertTrue(any("Galat internal" in e.value for e in at.error))
        self.assertTrue(any(m.label == "Total request" for m in at.metric), "ringkasan tetap tampil")

    def test_data_kosong_menampilkan_info_bukan_error(self):
        at, p_env, p_klien = app_baru()
        KlienPalsu.kosong = True
        with p_env, p_klien:
            at.run()
        self.assertFalse(at.exception)
        self.assertTrue(any("Belum ada request" in i.value for i in at.info))
        self.assertTrue(any("Tidak ada temuan guardrail" in s.value for s in at.success))

    def test_upstream_menampilkan_status_cooldown_dan_belum_dikonfigurasi(self):
        at = jalankan(*app_baru())
        metrik = {m.label: m.value for m in at.metric}
        self.assertEqual((metrik["Upstream"], metrik["Sedang cooldown"], metrik["Belum dikonfigurasi"]), ("3", "1", "1"))
        semua_teks = " ".join(str(d.value.to_dict()) for d in at.dataframe)
        self.assertIn("Cooldown 25 dtk", semua_teks)
        self.assertIn("Belum dikonfigurasi (env GROQ_API_KEY kosong)", semua_teks)
        self.assertIn("Sehat", semua_teks)

    def test_guardrail_menampilkan_konfigurasi_dan_kejadian_tanpa_isi_rahasia(self):
        at = jalankan(*app_baru())
        metrik = {m.label: m.value for m in at.metric}
        self.assertEqual((metrik["Status"], metrik["Mode bawaan"], metrik["Dipindai"]), ("Aktif", "redact", "request + respons"))
        self.assertTrue(any("github_token,jwt" in str(d.value.to_dict()) for d in at.dataframe))

    def test_buat_key_memanggil_api_dan_menampilkan_key_sekali(self):
        at = jalankan(*app_baru())
        at.text_input(key="baru_nama").set_value("tim-baru")
        at.checkbox(key="baru_rpm_ikut").set_value(False)
        at.number_input(key="baru_rpm").set_value(30)
        with mock.patch.dict(os.environ, {"NIGATE_ADMIN_TOKEN": "token-uji"}), mock.patch.object(klien, "KlienAdmin", KlienPalsu):
            next(b for b in at.button if b.label == "Buat key").click().run()
            self.assertIn(("buat", "tim-baru", 30, None), KlienPalsu.panggilan)
            self.assertTrue(any(c.value.startswith("ngk_") for c in at.code), "key baru ditampilkan")
            self.assertEqual(at.text_input(key="baru_nama").value, "", "input nama dikosongkan")
            next(b for b in at.button if b.label == "Sudah saya simpan").click().run()
            self.assertFalse(any(c.value.startswith("ngk_") for c in at.code), "key hilang setelah ditutup")

    def test_ubah_key_mengirim_null_saat_ikut_default(self):
        at = jalankan(*app_baru())
        at.selectbox(key="pilih_key").set_value("nirina-dev")
        with mock.patch.dict(os.environ, {"NIGATE_ADMIN_TOKEN": "token-uji"}), mock.patch.object(klien, "KlienAdmin", KlienPalsu):
            at.run()
            at.checkbox(key="rpm_ikut_nirina-dev").set_value(True)
            at.toggle(key="aktif_nirina-dev").set_value(True)
            next(b for b in at.button if b.label == "Simpan perubahan").click().run()
        self.assertIn(("ubah", "nirina-dev", True, None, 900), KlienPalsu.panggilan)

    def test_hapus_butuh_konfirmasi(self):
        at = jalankan(*app_baru())
        with mock.patch.dict(os.environ, {"NIGATE_ADMIN_TOKEN": "token-uji"}), mock.patch.object(klien, "KlienAdmin", KlienPalsu):
            next(b for b in at.button if b.label == "Hapus key").click().run()
            self.assertFalse([p for p in KlienPalsu.panggilan if p[0] == "hapus"], "tanpa centang tidak boleh menghapus")
            at.checkbox(key="yakin_nirina-prod").set_value(True)
            next(b for b in at.button if b.label == "Hapus key").click().run()
        self.assertIn(("hapus", "nirina-prod"), KlienPalsu.panggilan)

    def test_reload_melaporkan_bagian_yang_butuh_restart(self):
        at = jalankan(*app_baru())
        with mock.patch.dict(os.environ, {"NIGATE_ADMIN_TOKEN": "token-uji"}), mock.patch.object(klien, "KlienAdmin", KlienPalsu):
            next(b for b in at.button if b.label == "Muat ulang config dari file").click().run()
            self.assertIn(("reload",), KlienPalsu.panggilan)
            self.assertTrue(any("server.listen" in w.value for w in at.warning))
            self.assertTrue(any("dimuat ulang" in s.value for s in at.success))


if __name__ == "__main__":
    unittest.main()
