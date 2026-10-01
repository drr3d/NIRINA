"""Add-On tab: generic list of the add-ons the server discovered, an on/off toggle per add-on, and an apply/restart
panel. All reads and writes go through the Kelola API (addons.kelola.client); no add-on is special-cased and no
configuration file is touched from the Streamlit process.

Credentials follow the tool management tab: with guarded access the signed-in account is used, when the server runs in
local mode no key is needed, otherwise the operator types the management key (kept in this session only).
"""
import streamlit as st

from addons.kelola import client
from views.ruang_kerja import aman_markdown, bersihkan_teks

S_KUNCI = "t6_kunci"
S_PESAN = "addon_pesan"
S_PESAN_RESTART = "addon_restart_pesan"
S_TERTUNDA = "addon_tertunda"


def _terjaga():
    return bool(st.session_state.get("akses_terjaga"))


def _kunci_sesi():
    """Management key typed by the operator in this session (this tab or the tool management tab); None when the
    signed-in account is used."""
    if _terjaga():
        return None
    return (st.session_state.get(S_KUNCI) or st.session_state.get("t5_kunci") or "").strip() or None


def _bisa_ubah(data):
    mode = data.get("mode_kelola")
    if mode == "nonaktif":
        return False
    return mode == "lokal" or _terjaga() or _kunci_sesi() is not None


def _restart(instance_id):
    hasil = client.restart_sistem(instance_id, kunci=_kunci_sesi())
    if hasil.get("status") == "ok":
        st.session_state[S_PESAN_RESTART] = ("info",
            "Restart diterima. Menunggu pekerjaan selesai, lalu aplikasi tersambung kembali.")
    else:
        st.session_state[S_PESAN_RESTART] = ("warning",
            (hasil.get("pesan") or "Respons restart belum diterima.") +
            " Periksa status sebelum mencoba lagi; jangan tutup terminal NIRINA.")


@st.fragment(run_every="5s")
def _panel_restart():
    with st.container(border=True):
        st.markdown("**Terapkan perubahan**")
        pesan = st.session_state.get(S_PESAN_RESTART)
        if pesan:
            getattr(st, pesan[0])(aman_markdown(str(pesan[1])))
        tertunda = int(st.session_state.get(S_TERTUNDA) or 0)
        if tertunda:
            st.warning(f"{tertunda} perubahan menunggu restart.")
        data = client.status_sistem(kunci=_kunci_sesi())
        if data.get("status") != "ok":
            st.caption("Status restart belum tersedia. Aplikasi mungkin sedang memulai ulang atau akses ditolak.")
            return
        if not data.get("didukung"):
            st.caption("Restart dari sini hanya tersedia bila NIRINA dijalankan lewat python app.py. "
                       "Tanpa itu, mulai ulang aplikasi secara manual agar perubahan berlaku.")
        elif data.get("fase") == "menunggu_pekerjaan":
            st.info(f"Menunggu {data.get('pekerjaan_aktif', 0)} pekerjaan selesai. Pekerjaan baru ditahan sementara.")
        elif data.get("restart_id"):
            st.info("NIRINA sedang memulai ulang. Tunggu koneksi kembali; muat ulang halaman jika belum tersambung.")
        else:
            st.caption("Muat ulang API dan kanal NIRINA, termasuk pilihan add-on yang tertunda. "
                       "Pekerjaan aktif ditunggu.")
        st.button("Restart NIRINA", key="addon_restart", icon=":material/restart_alt:",
                  disabled=not data.get("didukung") or bool(data.get("restart_id")),
                  on_click=_restart, args=(data.get("instance_id"),))


def _simpan(nama, sidik, key, kunci, nilai_tampil):
    if bool(st.session_state.get(key)) == bool(nilai_tampil):
        # Not a user action: Streamlit calls on_change for stale widget state.
        return
    hasil = client.atur(nama, bool(st.session_state[key]), sidik, kunci=kunci)
    if hasil.get("status") == "ok":
        st.session_state[S_PESAN] = ("success",
            "Pilihan tersimpan. Berlaku setelah aplikasi dimulai ulang; status berjalan tidak berubah sampai itu.")
    else:
        st.session_state[S_PESAN] = ("error", hasil.get("pesan") or "Pilihan tidak dapat disimpan.")
    # Always rebuild the toggle from the server response, including after a rejection or timeout.
    st.session_state.pop(key, None)


def _label_status(item):
    aktif = item.get("aktif_sekarang")
    if not item.get("tersedia"):
        return "Tidak tersedia"
    return "Status tidak diketahui" if aktif is None else "Aktif" if aktif else "Nonaktif"


def _kartu(item, sidik, kunci, bisa_ubah):
    nama = bersihkan_teks(str(item.get("nama") or ""))
    if not nama:
        return
    with st.container(border=True):
        judul = aman_markdown(bersihkan_teks(item.get("judul")) or nama)
        versi = bersihkan_teks(item.get("versi"))
        st.markdown(f"**{judul}**" + (f" · v{aman_markdown(versi)}" if versi else ""))
        deskripsi = bersihkan_teks(item.get("deskripsi"))
        if deskripsi:
            st.caption(aman_markdown(deskripsi))
        st.caption("Sedang berjalan: " + _label_status(item))
        if item.get("tersedia") and item.get("bisa_diatur"):
            key = f"addon_{nama}_{sidik}"
            diminta = bool(item.get("aktif_diminta"))
            # Rebuild from the latest server state; never keep a stale browser choice.
            st.session_state[key] = diminta
            st.toggle("Aktif setelah restart", key=key, disabled=not bisa_ubah,
                      on_change=_simpan, args=(nama, sidik, key, kunci, diminta))
        if item.get("perlu_restart"):
            st.warning("Menunggu restart: akan " + ("aktif." if item.get("aktif_diminta") else "nonaktif."))
        alasan = bersihkan_teks(item.get("alasan"))
        if alasan:
            st.caption(aman_markdown(alasan))


def render():
    st.markdown("##### :material/extension: Add-On")
    st.caption("Kelola modul tambahan NIRINA. Perubahan disimpan sebagai pilihan tertunda dan berlaku setelah restart.")
    kunci = _kunci_sesi()
    st.button("Segarkan", key="addon_segarkan", icon=":material/refresh:")
    pesan = st.session_state.pop(S_PESAN, None)
    if pesan:
        getattr(st, pesan[0])(aman_markdown(str(pesan[1])))
    data = client.daftar(kunci=kunci)
    # The key box shows unless the server is in local mode or an account is signed in; it also stays visible while
    # the server still refuses the list, so the operator can supply it.
    if not _terjaga() and data.get("mode_kelola") != "lokal":
        st.text_input("Kunci kelola", key=S_KUNCI, type="password",
                      help="Kunci pengelola dari admin server (NIRINA_KELOLA_KUNCI). Hanya disimpan di sesi ini.")
    if data.get("status") != "ok":
        st.session_state[S_TERTUNDA] = 0
        st.warning("Daftar add-on belum tersedia. Pastikan server API berjalan dan Anda boleh mengelola add-on.")
        st.caption(aman_markdown(str(data.get("pesan") or "Coba segarkan setelah server siap.")))
        return
    bisa_ubah = _bisa_ubah(data)
    if data.get("mode_kelola") == "nonaktif" and not _terjaga():
        st.caption(":material/lock: Pengubahan nonaktif di server (kunci kelola belum diatur). Daftar tetap terlihat.")
    elif not bisa_ubah:
        st.caption(":material/key: Isi kunci kelola untuk mengubah pilihan add-on.")
    daftar = [r for r in (data.get("addons") or []) if isinstance(r, dict)]
    st.session_state[S_TERTUNDA] = sum(1 for r in daftar if r.get("perlu_restart"))
    _panel_restart()
    if not daftar:
        st.info("Belum ada add-on terpasang di server ini.")
        return
    for item in daftar:
        _kartu(item, str(data.get("sidik") or ""), kunci, bisa_ubah)
