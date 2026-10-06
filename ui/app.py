"""Dashboard nigate (Streamlit) — halaman mandiri yang berbicara ke API admin gateway lewat HTTP.

Jalankan:  python -m streamlit run app.py --server.port 8502     (atau ui\\jalankan.cmd di Windows)
Env:       NIGATE_ADMIN_URL (bawaan http://127.0.0.1:4001), NIGATE_ADMIN_TOKEN
Platform:  bila /platform/config.json ada (env NIGATE_PLATFORM_CONFIG), halaman hanya melayani request lewat login platform
           (header X-Platform-Proxy-Token) dan token admin diambil dari Pengaturan platform; lihat platform_cfg.py.
Halaman ini tidak membaca file database gateway; semua data lewat API admin.
"""

import os
import sys
from datetime import datetime

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd())
import klien  # noqa: E402
import platform_cfg  # noqa: E402
from klien import GalatGateway, TidakTerjangkau, TokenDitolak  # noqa: E402

st.set_page_config(page_title="nigate", page_icon=None, layout="wide")

PERIODE = {"1 jam": 1, "6 jam": 6, "24 jam": 24, "7 hari": 168, "30 hari": 720}
HASIL = ["ok", "klien", "limit", "guardrail", "upstream", "gateway"]
LABEL_HASIL = {
    "ok": "Sukses", "klien": "Salah klien", "limit": "Dibatasi", "guardrail": "Diblok guardrail",
    "upstream": "Error upstream", "gateway": "Error gateway",
}
WARNA_HASIL = ["#2e9e6b", "#e0a030", "#3b82c4", "#8b5cf6", "#d64545", "#7b8794"]
KOLOM_STATS = {
    "kelompok": "Kelompok", "request": "Request", "ok": "Sukses", "klien": "Salah klien", "limit": "Dibatasi",
    "guardrail": "Diblok guardrail", "upstream": "Error upstream", "gateway": "Error gateway", "temuan": "Temuan guardrail",
    "token_masuk": "Token masuk", "token_keluar": "Token keluar", "latensi_rata_ms": "Latensi rata-rata (ms)",
    "latensi_maks_ms": "Latensi maks (ms)",
}


# ---------- util ----------

def angka(n) -> str:
    return f"{int(n):,}".replace(",", ".")


def persen(bagian, total) -> str:
    return "-" if not total else f"{100 * bagian / total:.1f}%".replace(".", ",")


def waktu_lokal(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def durasi(detik: int) -> str:
    h, sisa = divmod(int(detik), 3600)
    m, d = divmod(sisa, 60)
    return f"{h} jam {m} mnt" if h else (f"{m} mnt {d} dtk" if m else f"{d} dtk")


def klien_admin() -> klien.KlienAdmin:
    return klien.KlienAdmin(st.session_state["_url"], st.session_state["_token"])


def aman(fungsi):
    """Menampilkan galat gateway sebagai pesan yang jelas alih-alih traceback."""

    def dibungkus(*arg, **kw):
        try:
            return fungsi(*arg, **kw)
        except TokenDitolak:
            st.error("Token admin ditolak oleh gateway. Periksa NIGATE_ADMIN_TOKEN.")
        except TidakTerjangkau as e:
            st.error(str(e))
        except GalatGateway as e:
            st.error(f"{e}")

    dibungkus.__name__ = fungsi.__name__
    return dibungkus


def notif(jenis: str, teks: str):
    st.session_state.setdefault("_notif", []).append((jenis, teks))


def tampilkan_notif():
    for jenis, teks in st.session_state.pop("_notif", []):
        {"ok": st.success, "error": st.error, "warn": st.warning}.get(jenis, st.info)(teks)


def jalankan_aksi(fungsi, sukses: str):
    """Untuk callback tombol: panggil API, simpan hasilnya sebagai notifikasi."""
    try:
        hasil = fungsi()
        notif("ok", sukses)
        return hasil
    except GalatGateway as e:
        notif("error", str(e))
        return None


def df_stats(baris: list) -> pd.DataFrame:
    return pd.DataFrame(baris).rename(columns=KOLOM_STATS)[list(KOLOM_STATS.values())] if baris else pd.DataFrame(columns=list(KOLOM_STATS.values()))


def tabel_stats(baris: list):
    df = df_stats(baris)
    st.dataframe(
        df, width="stretch", hide_index=True,
        column_config={"Latensi rata-rata (ms)": st.column_config.NumberColumn(format="%.0f")},
    )


# ---------- tab: ringkasan ----------

def deret_waktu(c: klien.KlienAdmin, jam: int) -> pd.DataFrame:
    """Baris per jam (UTC dari gateway) diubah ke zona waktu lokal; periode panjang dijumlah per hari lokal."""
    baris = c.stats(jam, "jam")["baris"]
    if not baris:
        return pd.DataFrame()
    df = pd.DataFrame(baris)
    df["waktu"] = pd.to_datetime(df["kelompok"], utc=True).dt.tz_convert(datetime.now().astimezone().tzinfo).dt.tz_localize(None)
    df["_bobot"] = df["latensi_rata_ms"] * df["request"]
    satuan = "h" if jam <= 48 else "D"
    df["waktu"] = df["waktu"].dt.floor(satuan)
    agregat = df.groupby("waktu")[HASIL + ["request", "_bobot", "token_masuk", "token_keluar"]].sum()
    agregat["latensi"] = (agregat["_bobot"] / agregat["request"]).fillna(0)
    akhir = pd.Timestamp.now().floor(satuan)
    rentang = pd.date_range(end=akhir, periods=min(jam if satuan == "h" else jam // 24, 90), freq=satuan)
    return agregat.reindex(rentang, fill_value=0)


@aman
def tab_ringkasan(c: klien.KlienAdmin, jam: int, label: str):
    semua = c.stats(jam, "semua")["baris"]
    if not semua:
        st.info(f"Belum ada request tercatat pada periode {label}.")
        return
    t = semua[0]
    a = st.columns(4)
    a[0].metric("Total request", angka(t["request"]))
    a[1].metric("Berhasil", persen(t["ok"], t["request"]), help="Request yang dijawab sukses (status < 400).")
    a[2].metric("Dibatasi (rate limit)", angka(t["limit"]), help="Ditolak 429 karena melewati RPM/TPM key.")
    a[3].metric("Diblok guardrail", angka(t["guardrail"]), help="Ditolak karena mengandung data sensitif (mode block).")
    b = st.columns(4)
    b[0].metric("Error upstream", angka(t["upstream"]), help="Provider gagal, timeout, atau semua upstream habis.")
    b[1].metric("Temuan guardrail", angka(t["temuan"]), help="Jumlah secret yang dideteksi (diredaksi, diblok, atau dicatat).")
    b[2].metric("Token masuk / keluar", f"{angka(t['token_masuk'])} / {angka(t['token_keluar'])}")
    b[3].metric("Latensi rata-rata", f"{t['latensi_rata_ms']:.0f} ms", help=f"Terlama: {angka(t['latensi_maks_ms'])} ms")

    df = deret_waktu(c, jam)
    if not df.empty:
        st.markdown("##### Request per " + ("jam" if jam <= 48 else "hari"))
        grafik = df[HASIL].rename(columns=LABEL_HASIL)
        st.bar_chart(grafik, color=WARNA_HASIL, stack=True, height=260)
        kiri, kanan = st.columns(2)
        with kiri:
            st.markdown("##### Latensi rata-rata (ms)")
            st.line_chart(df[["latensi"]].rename(columns={"latensi": "Latensi (ms)"}), height=200, color="#3b82c4")
        with kanan:
            st.markdown("##### Token")
            st.bar_chart(df[["token_masuk", "token_keluar"]].rename(columns={"token_masuk": "Masuk", "token_keluar": "Keluar"}),
                         color=["#2e9e6b", "#8b5cf6"], height=200)

    st.markdown("##### Rincian")
    for nama, per in (("Per key", "key"), ("Per client (key/user)", "user"), ("Per model (alias)", "alias"), ("Per upstream", "upstream")):
        with st.expander(nama, expanded=(per == "key")):
            tabel_stats(c.stats(jam, per)["baris"])


# ---------- tab: key & limit ----------

BANTUAN_METADATA = (
    "Satu baris per label, format nama=nilai, mis. client_id=mall-a. Gateway tidak menafsirkannya; aplikasi di depan gateway "
    "(mis. API NIRINA) membacanya lewat GET /v1/key/info untuk mengetahui milik siapa key ini."
)


def baca_metadata(teks: str) -> dict:
    """Teks 'nama=nilai' per baris menjadi dict. Baris kosong diabaikan; validasi lengkap dilakukan gateway."""
    hasil = {}
    for no, baris in enumerate((teks or "").splitlines(), 1):
        baris = baris.strip()
        if not baris:
            continue
        nama, sep, nilai = baris.partition("=")
        if not sep or not nama.strip():
            raise ValueError(f"Metadata baris {no} harus berbentuk nama=nilai.")
        hasil[nama.strip()] = nilai.strip()
    return hasil


def tulis_metadata(m: dict) -> str:
    return "\n".join(f"{k}={v}" for k, v in (m or {}).items())


def _cb_buat():
    nama = st.session_state.get("baru_nama", "").strip()
    rpm = None if st.session_state.get("baru_rpm_ikut", True) else int(st.session_state["baru_rpm"])
    tpm = None if st.session_state.get("baru_tpm_ikut", True) else int(st.session_state["baru_tpm"])
    try:
        meta = baca_metadata(st.session_state.get("baru_meta", ""))
    except ValueError as e:
        notif("error", str(e))
        return
    hasil = jalankan_aksi(lambda: klien_admin().buat_key(nama, rpm, tpm, meta), f"Key '{nama}' dibuat.")
    if hasil:
        st.session_state["key_baru"] = (nama, hasil["key"])
        st.session_state["baru_nama"] = ""
        st.session_state["baru_meta"] = ""


def _cb_simpan(nama: str):
    rpm = None if st.session_state[f"rpm_ikut_{nama}"] else int(st.session_state[f"rpm_{nama}"])
    tpm = None if st.session_state[f"tpm_ikut_{nama}"] else int(st.session_state[f"tpm_{nama}"])
    aktif = bool(st.session_state[f"aktif_{nama}"])
    try:
        meta = baca_metadata(st.session_state.get(f"meta_{nama}", "")) or None
    except ValueError as e:
        notif("error", str(e))
        return
    jalankan_aksi(
        lambda: klien_admin().ubah_key(nama, active=aktif, rpm=rpm, tpm=tpm, metadata=meta), f"Perubahan pada '{nama}' disimpan."
    )


def _cb_hapus(nama: str):
    if not st.session_state.get(f"yakin_{nama}"):
        notif("warn", "Centang konfirmasi dulu sebelum menghapus.")
        return
    jalankan_aksi(lambda: klien_admin().hapus_key(nama), f"Key '{nama}' dihapus permanen.")


def _cb_tutup_key_baru():
    st.session_state.pop("key_baru", None)


@aman
def tab_key(c: klien.KlienAdmin, cfg_limit: dict):
    tampilkan_notif()
    if "key_baru" in st.session_state:
        nama, key = st.session_state["key_baru"]
        st.success(f"Key untuk '{nama}' berhasil dibuat.")
        st.code(key, language=None)
        st.warning("Simpan key ini sekarang. Gateway hanya menyimpan hash-nya, jadi key ini tidak bisa ditampilkan lagi.")
        st.button("Sudah saya simpan", on_click=_cb_tutup_key_baru)

    keys = c.keys()
    if keys:
        df = pd.DataFrame([{
            "Nama": k["name"], "Awalan": k["prefix"] + "…", "Status": "Aktif" if k["active"] else "Dicabut",
            "RPM": angka(k["rpm_efektif"]) if k["rpm_efektif"] is not None else "tanpa batas",
            "TPM": angka(k["tpm_efektif"]) if k["tpm_efektif"] is not None else "tanpa batas",
            "Batas sendiri": "ya" if (k["rpm"] is not None or k["tpm"] is not None) else "ikut default",
            "Metadata": ", ".join(f"{a}={b}" for a, b in (k.get("metadata") or {}).items()) or "-",
            "Dibuat": datetime.fromtimestamp(k["created_at"]).strftime("%Y-%m-%d %H:%M"),
        } for k in keys])
        st.dataframe(df, width="stretch", hide_index=True)
    else:
        st.info("Belum ada key. Buat key pertama di bawah.")
    d = [cfg_limit.get("default_rpm"), cfg_limit.get("default_tpm")]
    st.caption(f"Batas bawaan gateway: RPM {d[0] or 'tanpa batas'}, TPM {d[1] or 'tanpa batas'}. Key yang tidak punya batas sendiri mengikuti ini.")

    kiri, kanan = st.columns(2)
    with kiri:
        st.markdown("##### Buat key baru")
        st.text_input("Nama key", key="baru_nama", placeholder="mis. nirina-prod", help="Huruf, angka, _ - . (maks. 64 karakter)")
        st.checkbox("RPM ikut default", value=True, key="baru_rpm_ikut")
        st.number_input("Request per menit (RPM)", min_value=1, value=60, step=1, key="baru_rpm", disabled=st.session_state.get("baru_rpm_ikut", True))
        st.checkbox("TPM ikut default", value=True, key="baru_tpm_ikut")
        st.number_input("Token per menit (TPM)", min_value=1, value=100000, step=1000, key="baru_tpm", disabled=st.session_state.get("baru_tpm_ikut", True))
        st.text_area("Metadata (opsional)", key="baru_meta", placeholder="client_id=mall-a", height=80, help=BANTUAN_METADATA)
        st.button("Buat key", type="primary", on_click=_cb_buat)

    with kanan:
        st.markdown("##### Ubah atau cabut key")
        if not keys:
            st.caption("Belum ada key untuk diubah.")
            return
        nama = st.selectbox("Pilih key", [k["name"] for k in keys], key="pilih_key")
        k = next(x for x in keys if x["name"] == nama)
        st.toggle("Aktif", value=k["active"], key=f"aktif_{nama}", help="Matikan untuk mencabut key (bisa diaktifkan lagi).")
        st.checkbox("RPM ikut default", value=k["rpm"] is None, key=f"rpm_ikut_{nama}")
        st.number_input("RPM", min_value=1, value=int(k["rpm"] or 60), step=1, key=f"rpm_{nama}", disabled=st.session_state.get(f"rpm_ikut_{nama}", k["rpm"] is None))
        st.checkbox("TPM ikut default", value=k["tpm"] is None, key=f"tpm_ikut_{nama}")
        st.number_input("TPM", min_value=1, value=int(k["tpm"] or 100000), step=1000, key=f"tpm_{nama}", disabled=st.session_state.get(f"tpm_ikut_{nama}", k["tpm"] is None))
        st.text_area("Metadata", value=tulis_metadata(k.get("metadata")), key=f"meta_{nama}", height=80, help=BANTUAN_METADATA + " Kosongkan untuk menghapus.")
        st.button("Simpan perubahan", on_click=_cb_simpan, args=(nama,))
        with st.expander("Hapus permanen"):
            st.caption("Menghapus key dari gateway. Riwayat statistik tetap ada. Untuk sekadar menonaktifkan, matikan 'Aktif' di atas.")
            st.checkbox(f"Ya, hapus '{nama}'", key=f"yakin_{nama}")
            st.button("Hapus key", on_click=_cb_hapus, args=(nama,))

    st.divider()
    bagian_client(c, k)


# ---------- client (label `user`) di bawah key ----------

BANTUAN_CLIENT = (
    "Aplikasi yang melayani banyak client lewat satu key (mis. API NIRINA) mengirim label client di field standar OpenAI "
    "`user`. Gateway membatasi dan mencatat pemakaian per label itu, di bawah batas key, dan tidak meneruskannya ke provider."
)


def _batas_dari(prefiks: str):
    return None if st.session_state.get(f"{prefiks}_bebas", True) else int(st.session_state[prefiks])


def _cb_aturan_client(nama: str):
    rpm, tpm = _batas_dari(f"crpm_{nama}"), _batas_dari(f"ctpm_{nama}")
    wajib = bool(st.session_state.get(f"cwajib_{nama}", False))
    jalankan_aksi(lambda: klien_admin().ubah_key(nama, user_rpm=rpm, user_tpm=tpm, user_required=wajib),
                  f"Aturan client untuk '{nama}' disimpan.")


def _cb_simpan_client(nama: str):
    user = (st.session_state.get(f"cuser_{nama}") or "").strip()
    if not user:
        notif("error", "Isi label client dulu (nilai field user yang dikirim aplikasi).")
        return
    rpm = None if st.session_state.get(f"cu_rpm_ikut_{nama}", True) else int(st.session_state[f"cu_rpm_{nama}"])
    tpm = None if st.session_state.get(f"cu_tpm_ikut_{nama}", True) else int(st.session_state[f"cu_tpm_{nama}"])
    aktif = bool(st.session_state.get(f"cu_aktif_{nama}", True))
    if jalankan_aksi(lambda: klien_admin().simpan_client(nama, user, rpm, tpm, aktif), f"Client '{user}' disimpan."):
        st.session_state[f"cuser_{nama}"] = ""


def _cb_hapus_client(nama: str):
    user = st.session_state.get(f"chapus_{nama}")
    if user:
        jalankan_aksi(lambda: klien_admin().hapus_client(nama, user), f"Pengaturan khusus client '{user}' dihapus.")


def _input_batas(label: str, prefiks: str, nilai, bawaan: int, langkah: int, teks_bebas: str):
    st.checkbox(teks_bebas, value=nilai is None, key=f"{prefiks}_bebas")
    st.number_input(label, min_value=1, value=int(nilai or bawaan), step=langkah, key=prefiks,
                    disabled=st.session_state.get(f"{prefiks}_bebas", nilai is None))


def bagian_client(c: klien.KlienAdmin, k: dict):
    nama = k["name"]
    st.markdown(f"##### Client di bawah key '{nama}'", help=BANTUAN_CLIENT)
    data = c.clients(nama)
    kiri, kanan = st.columns(2)
    with kiri:
        st.caption("Aturan untuk semua client key ini")
        st.checkbox("Wajib kirim label client (user)", value=bool(k.get("user_required")), key=f"cwajib_{nama}",
                    help="Request tanpa field user ditolak 400 user_required, supaya semua pemakaian teratribusi ke client.")
        _input_batas("RPM per client", f"crpm_{nama}", k.get("user_rpm"), 30, 1, "RPM per client: tanpa batas")
        _input_batas("TPM per client", f"ctpm_{nama}", k.get("user_tpm"), 50000, 1000, "TPM per client: tanpa batas")
        st.button("Simpan aturan client", on_click=_cb_aturan_client, args=(nama,))
    with kanan:
        st.caption("Pengaturan khusus satu client (mengalahkan aturan di kiri)")
        st.text_input("Label client", key=f"cuser_{nama}", placeholder="mis. mall-a",
                      help="Persis nilai field user dari aplikasi. Huruf, angka, _ - . : @ (maks. 64).")
        st.checkbox("RPM ikut aturan key", value=True, key=f"cu_rpm_ikut_{nama}")
        st.number_input("RPM client", min_value=1, value=30, step=1, key=f"cu_rpm_{nama}",
                        disabled=st.session_state.get(f"cu_rpm_ikut_{nama}", True))
        st.checkbox("TPM ikut aturan key", value=True, key=f"cu_tpm_ikut_{nama}")
        st.number_input("TPM client", min_value=1, value=50000, step=1000, key=f"cu_tpm_{nama}",
                        disabled=st.session_state.get(f"cu_tpm_ikut_{nama}", True))
        st.toggle("Aktif", value=True, key=f"cu_aktif_{nama}", help="Matikan untuk memblokir client ini (403 user_blocked).")
        st.button("Simpan client", on_click=_cb_simpan_client, args=(nama,))

    daftar = data.get("users") or []
    if daftar:
        st.dataframe(pd.DataFrame([{
            "Client": u["user"], "Status": "Aktif" if u["active"] else "Diblokir",
            "RPM": angka(u["rpm_efektif"]) if u["rpm_efektif"] is not None else "tanpa batas",
            "TPM": angka(u["tpm_efektif"]) if u["tpm_efektif"] is not None else "tanpa batas",
            "Batas sendiri": "ya" if (u["rpm"] is not None or u["tpm"] is not None) else "ikut aturan key",
        } for u in daftar]), width="stretch", hide_index=True)
        with st.expander("Hapus pengaturan khusus client"):
            st.selectbox("Client", [u["user"] for u in daftar], key=f"chapus_{nama}")
            st.caption("Client kembali memakai aturan key. Riwayat statistik tetap ada.")
            st.button("Hapus pengaturan client", on_click=_cb_hapus_client, args=(nama,))
    else:
        st.caption("Belum ada client dengan pengaturan khusus. Client lain tetap dibatasi aturan di kiri dan tercatat di Ringkasan > Per client.")


# ---------- tab: upstream ----------

@aman
def tab_upstream(c: klien.KlienAdmin):
    ups = c.upstreams()
    if not ups:
        st.info("Belum ada model yang dikonfigurasi.")
        return
    dingin = sum(1 for u in ups if u["dalam_cooldown"])
    belum = sum(1 for u in ups if not u["terkonfigurasi"])
    a = st.columns(3)
    a[0].metric("Upstream", len(ups))
    a[1].metric("Sedang cooldown", dingin, help="Dilewati sementara karena gagal; tetap dicoba bila tak ada pilihan lain.")
    a[2].metric("Belum dikonfigurasi", belum, help="Variabel environment key providernya kosong; dilewati.")
    st.caption("Urutan = prioritas failover. Upstream berikutnya dipakai bila yang sebelumnya gagal.")

    def status(u):
        if not u["terkonfigurasi"]:
            return f"Belum dikonfigurasi (env {u['key_env']} kosong)"
        return f"Cooldown {u['sisa_cooldown_detik']} dtk" if u["dalam_cooldown"] else "Sehat"

    for alias in dict.fromkeys(u["alias"] for u in ups):
        st.markdown(f"**{alias}**")
        st.dataframe(pd.DataFrame([{
            "Urutan": u["urutan"], "Nama": u["name"], "Model asli": u["model"], "Endpoint": u["url"],
            "Status": status(u), "Gagal beruntun": u["gagal_beruntun"], "Timeout (dtk)": u["timeout_detik"],
        } for u in ups if u["alias"] == alias]), width="stretch", hide_index=True)


# ---------- tab: guardrail ----------

@aman
def tab_guardrail(c: klien.KlienAdmin, jam: int, label: str):
    g = c.config()["guardrail"]
    a = st.columns(4)
    a[0].metric("Status", "Aktif" if g["enabled"] else "Nonaktif")
    a[1].metric("Mode bawaan", g["mode"], help="redact = ganti jadi [REDACTED:aturan]; block = tolak; log_only = hanya catat.")
    a[2].metric("Dipindai", " + ".join(x for x, on in (("request", g["scan_request"]), ("respons", g["scan_response"])) if on) or "-")
    a[3].metric("Deteksi entropi", "Aktif" if g["entropy"] else "Nonaktif", help=f"Ambang {g['entropy_threshold']} bit/karakter, min. {g['entropy_min_length']} karakter.")
    if g["aksi"] or g["rule_kustom"]:
        with st.expander("Aturan dengan mode khusus / aturan tambahan"):
            if g["aksi"]:
                st.dataframe(pd.DataFrame([{"Aturan": k, "Mode": v} for k, v in g["aksi"].items()]), width="stretch", hide_index=True)
            if g["rule_kustom"]:
                st.dataframe(pd.DataFrame([{"Aturan kustom": r["name"], "Mode": r["mode"] or "(bawaan)"} for r in g["rule_kustom"]]), width="stretch", hide_index=True)

    st.markdown(f"##### Kejadian terbaru ({label})")
    kejadian = c.kejadian_guardrail(jam, 200)
    if not kejadian:
        st.success("Tidak ada temuan guardrail pada periode ini.")
        return
    df = pd.DataFrame([{
        "Waktu": waktu_lokal(k["ts_ms"]), "Key": k["key_name"], "Model": k["alias"] or "-", "Hasil": LABEL_HASIL.get(k["hasil"], k["hasil"]),
        "Temuan di request": k["temuan_masuk"], "Temuan di respons": k["temuan_keluar"], "Jenis": k["jenis_temuan"] or "-",
    } for k in kejadian])
    hitung = {}
    for k in kejadian:
        for jenis in (k["jenis_temuan"] or "").split(","):
            if jenis:
                hitung[jenis] = hitung.get(jenis, 0) + k["temuan_masuk"] + k["temuan_keluar"]
    if hitung:
        st.bar_chart(pd.Series(hitung, name="Temuan").sort_values(ascending=False), horizontal=True, color="#8b5cf6", height=max(120, 34 * len(hitung)))
    st.dataframe(df, width="stretch", hide_index=True)
    st.caption("Hanya jenis aturan dan jumlahnya yang dicatat; isi rahasia tidak pernah disimpan.")


# ---------- tab: konfigurasi ----------

def _cb_reload():
    hasil = jalankan_aksi(lambda: klien_admin().reload(), "Config dimuat ulang tanpa restart.")
    if hasil and hasil.get("perlu_restart"):
        notif("warn", "Perubahan pada bagian ini butuh restart dan belum berlaku: " + ", ".join(hasil["perlu_restart"]))


@aman
def tab_konfigurasi(c: klien.KlienAdmin, health: dict):
    tampilkan_notif()
    st.markdown("Perubahan yang dibuat di file `nigate.toml` (model, upstream, resilience, limit bawaan, guardrail) dapat diterapkan tanpa restart.")
    st.button("Muat ulang config dari file", type="primary", on_click=_cb_reload, disabled=not health.get("reload_tersedia"),
              help=None if health.get("reload_tersedia") else "Gateway tidak dijalankan dari file config.")
    st.caption("Config yang tidak valid ditolak seluruhnya dan yang lama tetap berjalan. Alamat listen, path database, dan pengaturan admin butuh restart.")
    with st.expander("Config yang sedang berjalan (tanpa rahasia)", expanded=True):
        st.json(c.config())


# ---------- halaman ----------

def koneksi_platform(cfg: dict):
    """(url, token) di bawah platform, atau None bila halaman tidak boleh/bisa dilanjutkan (pesannya sudah ditampilkan)."""
    if not platform_cfg.proxy_token(cfg):
        st.error("Dashboard belum terhubung ke platform: token proxy belum ada. Buka tim nigate di halaman setup platform sekali, "
                 "lalu tunggu sekitar 10 detik.")
        return None
    if not platform_cfg.header_sah(platform_cfg.header_proxy(), cfg):
        st.error("Akses ditolak. Buka dashboard ini lewat halaman platform (menu tim nigate).")
        return None
    st.sidebar.caption("Terhubung lewat platform. Token admin diambil dari Pengaturan platform.")
    url = os.environ.get("NIGATE_ADMIN_URL", "").strip() or platform_cfg.URL_ADMIN_PLATFORM
    token = platform_cfg.token_admin(cfg) or os.environ.get("NIGATE_ADMIN_TOKEN", "").strip()
    if not token:
        st.title("nigate")
        st.info("Token admin belum diisi. Isi NIGATE_ADMIN_TOKEN di Pengaturan tim nigate di platform, lalu Restart gateway di tab Service.")
        return None
    return url, token


def main():
    st.sidebar.title("nigate")
    st.sidebar.caption("AI gateway untuk NIRINA")
    try:
        cfg_platform = platform_cfg.baca()
    except platform_cfg.ConfigRusak as e:
        st.error(f"{e} Simpan ulang Pengaturan tim nigate di platform.")
        return
    if cfg_platform is not None:
        hasil = koneksi_platform(cfg_platform)
        if hasil is None:
            return
        url, token = hasil
    else:
        url = st.sidebar.text_input("Alamat API admin", value=os.environ.get("NIGATE_ADMIN_URL", "http://127.0.0.1:4001"))
        token = os.environ.get("NIGATE_ADMIN_TOKEN", "").strip()
        if token:
            st.sidebar.caption("Token admin dibaca dari environment.")
        else:
            token = st.sidebar.text_input("Token admin", type="password", help="Buat dengan: nigate admin token").strip()
    label = st.sidebar.selectbox("Periode", list(PERIODE), index=2)
    otomatis = st.sidebar.toggle("Segarkan otomatis (10 dtk)", value=False)
    st.sidebar.button("Segarkan sekarang")
    st.session_state["_url"], st.session_state["_token"] = url, token

    if not token:
        st.title("nigate")
        st.info("Masukkan token admin di sidebar untuk terhubung ke gateway.")
        return
    try:
        c = klien.KlienAdmin(url, token)
        health = c.health()
        cfg_limit = c.config()["limits"]
    except ValueError as e:
        st.error(str(e))
        return
    except TokenDitolak:
        st.error("Token admin ditolak oleh gateway. Periksa token di sidebar / NIGATE_ADMIN_TOKEN.")
        return
    except TidakTerjangkau as e:
        st.error(str(e))
        st.caption("Pastikan gateway berjalan dan API admin aktif (butuh env NIGATE_ADMIN_TOKEN di sisi gateway).")
        return
    except GalatGateway as e:
        st.error(str(e))
        return

    st.sidebar.divider()
    st.sidebar.caption(f"Gateway v{health['versi']} · aktif {durasi(health['uptime_detik'])}")
    if not health["stats_aktif"]:
        st.sidebar.warning("Statistik dimatikan di gateway.")
    if health["statistik_dibuang"]:
        st.sidebar.warning(f"{angka(health['statistik_dibuang'])} rekaman statistik terbuang (antrian penuh).")
    st.sidebar.caption("Statistik hanya metadata (tanpa isi prompt/jawaban).")

    jam = PERIODE[label]

    st.title("nigate")
    t1, t2, t3, t4, t5 = st.tabs(["Ringkasan", "Key & Limit", "Upstream", "Guardrail", "Konfigurasi"])

    def segar(fungsi, *arg):
        (st.fragment(run_every="10s")(fungsi) if otomatis else fungsi)(*arg)

    with t1:
        segar(tab_ringkasan, c, jam, label)
    with t2:
        tab_key(c, cfg_limit)
    with t3:
        segar(tab_upstream, c)
    with t4:
        segar(tab_guardrail, c, jam, label)
    with t5:
        tab_konfigurasi(c, health)


main()
