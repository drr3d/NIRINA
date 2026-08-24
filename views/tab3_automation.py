import streamlit as st
import json
import datetime
import requests

from database import automation_db as db
from core_agent.registry import ToolRegistry

db.init_db()

# [KONFIG] Alamat daemon Flask -- port SENGAJA beda dari port aplikasi lain
# di project ini (mis. localhost:5000 yang dipakai target pentest kamu di
# percakapan sebelumnya) biar gak bentrok.
DAEMON_URL = "http://127.0.0.1:5055"


# ==========================================
# HELPER: BACA SKEMA ARGUMEN 1 TOOL
# ==========================================
def _ambil_tool_by_name(nama: str):
    """Cari 1 tool object dari kategori 'safe' (sama dengan sumber dropdown)
    berdasarkan namanya. Return None kalau gak ketemu (mis. tool sudah
    dihapus/plugin-nya di-nonaktifkan sejak automation ini dibuat)."""
    return next((t for t in ToolRegistry.get_tools("safe") if t.name == nama), None)


def _skema_param(tool_obj) -> dict:
    """ Baca skema argumen tool langsung dari objek LangChain-nya
    (tool.args -- auto-generate dari type hints function aslinya, TIDAK perlu
    didaftarkan manual di file ini). Return dict {nama_param: tipe_str}."""
    if tool_obj is None:
        return {}
    try:
        return {nama: info.get("type", "string") for nama, info in (tool_obj.args or {}).items()}
    except Exception:
        return {}


def _reorder_aman(steps: list, idx_a: int, idx_b: int) -> bool:
    """ Simulasikan swap step idx_a & idx_b, cek SEMUA referensi
    'from_step' (step tool) DAN 'sumber_step' (step ai_transform) di seluruh
    alur masih menunjuk ke step SEBELUM dirinya setelah swap. Return False
    kalau ada step yang jadinya "meminta data dari masa depan" -- itu yang
    bikin tombol ▲▼ di-disable/ditolak, biar draft gak pernah nyampe ke
    state yang gak valid.

     Juga cegah reorder yang nyentuh index manapun di ANTARA sumber
    dan tujuan lompatan manapun -- 'lompat_jika.ke_step' itu index ABSOLUT,
    kalau urutan digeser tapi angkanya gak ikut nyesuaian, lompatan bisa
    diem-diem nunjuk ke step yang SALAH tanpa ada error apapun."""
    simulasi = list(steps)
    simulasi[idx_a], simulasi[idx_b] = simulasi[idx_b], simulasi[idx_a]
    for i, step in enumerate(simulasi):
        if step.get("type") == "ai_transform":
            sumber = step.get("sumber_step")
            if sumber is not None and sumber >= i:
                return False
        for v in (step.get("args") or {}).values():
            if isinstance(v, dict) and v.get("type") == "from_step" and v["step"] >= i:
                return False

    for idx_src, step in enumerate(steps):
        lompat = step.get("lompat_jika")
        if lompat and lompat.get("ke_step") is not None:
            lo, hi = min(idx_src, lompat["ke_step"]), max(idx_src, lompat["ke_step"])
            if lo <= idx_a <= hi or lo <= idx_b <= hi:
                return False
    return True


def _label_step(step: dict, index: int) -> str:
    """ Label ringkas 1 step, aman buat SEMUA jenis step (tool ATAU
    ai_transform) -- dipakai di preview, dropdown 'ambil dari step mana', dan
    panel detail, biar gak ada 1 tempat pun yang asumsi step['tool'] selalu ada."""
    if step.get("type") == "ai_transform":
        sumber = step.get("sumber_step")
        return f"Step {index+1}: 🤖 AI Transform (dari step {sumber+1 if sumber is not None else '?'})"
    return f"Step {index+1}: {step.get('tool', '?')}"


def _hitung_step_kondisional(steps: list) -> set:
    """ Return set index step yang BISA DILEWATI (gak selalu jalan)
    karena ada di rentang lompatan step lain. Dipakai buat kasih indikator
    visual "step ini kondisional, bukan wajib jalan" -- biar orang yang baca
    alur ini (bukan cuma yang bikin) gak salah kira semua step pasti
    dieksekusi tiap run."""
    kondisional = set()
    for i, step in enumerate(steps):
        lompat = step.get("lompat_jika")
        if lompat and lompat.get("ke_step") is not None:
            for j in range(i + 1, lompat["ke_step"]):
                kondisional.add(j)
    return kondisional


def _tampilkan_ringkasan_kondisional(steps: list):
    """ Banner ringkasan di atas daftar step -- "N dari M step SELALU
    jalan" -- render sekali di atas, sebelum daftar step satu-satu."""
    kondisional = _hitung_step_kondisional(steps)
    if kondisional:
        selalu = len(steps) - len(kondisional)
        st.info(
            f"🔀 **Alur ini punya percabangan.** {selalu} dari {len(steps)} step SELALU jalan tiap run; "
            f"{len(kondisional)} step lainnya (ditandai ⏭️ di bawah) CUMA jalan di skenario tertentu, "
            f"bisa dilewati tergantung hasil step sebelumnya."
        )


def _pecah_interval(detik: int) -> tuple:
    """ Kebalikan dari perhitungan 'value x satuan -> total detik' yang
    dipakai pas SIMPAN -- dipakai buat prefill form EDIT (INTERVAL) supaya
    gak nampilin '3600 Detik', tapi '1 Jam'."""
    if detik % 86400 == 0 and detik >= 86400:
        return detik // 86400, "Hari"
    if detik % 3600 == 0 and detik >= 3600:
        return detik // 3600, "Jam"
    if detik % 60 == 0 and detik >= 60:
        return detik // 60, "Menit"
    return detik, "Detik"


def _render_input_manual(nama_param: str, tipe: str, key: str):
    """Render widget input yang sesuai tipe parameter -- dipanggil pas user
    pilih 'Isi Manual' buat 1 argumen."""
    if tipe == "boolean":
        return st.checkbox(nama_param, key=key)
    elif tipe in ("integer",):
        return st.number_input(nama_param, step=1, key=key)
    elif tipe in ("number",):
        return st.number_input(nama_param, key=key)
    else:  # string, atau tipe lain yang gak dikenal -> treat sebagai teks bebas
        return st.text_input(nama_param, key=key)


def render():
    st.header("⚡ Automation Builder")

    # ==========================================
    # STATUS DAEMON (ping cepat, gak blocking lama kalau daemon mati)
    # ==========================================
    col_status, _ = st.columns([2, 3])
    with col_status:
        try:
            resp = requests.get(f"{DAEMON_URL}/status", timeout=1.5)
            if resp.status_code == 200:
                info = resp.json()
                st.success(f"🟢 Daemon aktif — {info.get('automation_aktif', '?')} automation RUNNING dipantau")
            else:
                st.warning("🟡 Daemon merespons tapi statusnya gak normal.")
        except requests.exceptions.RequestException:
            st.error(f"🔴 Daemon tidak terhubung ({DAEMON_URL}). Jalankan `python automation_daemon.py` dulu.")

    # ==========================================
    # BAGIAN A: DAFTAR AUTOMATION (READ, UPDATE, DELETE)
    # ==========================================
    st.subheader("📋 Daftar Automation Aktif")
    daftar_auto = db.ambil_semua_automation()

    if not daftar_auto:
        st.info("Belum ada automation yang dibuat. Silakan buat baru di bawah.")
    else:
        import pandas as pd

        df_data = []
        for auto in daftar_auto:
            if auto['tipe_jadwal'] == "INTERVAL":
                detik = int(auto['waktu_eksekusi'])
                if detik % 86400 == 0: info_waktu = f"Tiap {detik // 86400} Hari"
                elif detik % 3600 == 0: info_waktu = f"Tiap {detik // 3600} Jam"
                elif detik % 60 == 0: info_waktu = f"Tiap {detik // 60} Menit"
                else: info_waktu = f"Tiap {detik} Detik"
            else:
                info_waktu = f"Pukul {auto['waktu_eksekusi']}"

            last_run = auto.get('last_run_at')
            info_last_run = (
                datetime.datetime.fromtimestamp(last_run).strftime("%d/%m %H:%M")
                if last_run else "Belum pernah"
            )

            df_data.append({
                "ID": auto['id'],
                "Status": "🟢 RUNNING" if auto['status'] == "RUNNING" else "⚪ STOPPED",
                "Nama Automation": auto['nama_alur'],
                "Jadwal": info_waktu,
                "Terakhir Jalan": info_last_run,
            })

        df = pd.DataFrame(df_data)

        st.caption("💡 *Tip: Klik pada baris tabel untuk melihat detail alur, riwayat eksekusi, dan menu aksi.*")

        event = st.dataframe(
            df[["Status", "Nama Automation", "Jadwal", "Terakhir Jalan"]],
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row"
        )

        selected_rows = event.selection.rows

        if selected_rows:
            selected_idx = selected_rows[0]
            selected_id = df.iloc[selected_idx]["ID"]
            auto_detail = next((a for a in daftar_auto if a['id'] == selected_id), None)

            if auto_detail:
                st.write("---")
                st.subheader(f"🔍 Panel Aksi: {auto_detail['nama_alur']}")

                # --- Render alur, dukung format LAMA (list of string) & BARU (list of dict) ---
                langkah = json.loads(auto_detail['steps_json'])
                if langkah and isinstance(langkah[0], str):
                    # Format lama, dari sebelum refactor -- cuma nama tool, gak ada argumen
                    st.caption("⚠️ Automation ini masih format LAMA (tanpa argumen tersimpan) -- buat ulang lewat form di bawah kalau mau argumennya presisi.")
                    alur_teks = " ➡️ ".join([f"`{l}`" for l in langkah])
                    st.write(f"**Alur Eksekusi:** {alur_teks}")
                else:
                    _tampilkan_ringkasan_kondisional(langkah)
                    step_kondisional = _hitung_step_kondisional(langkah)
                    for i, step in enumerate(langkah):
                        stop_if = step.get("stop_if_output_contains")
                        if step.get("type") == "ai_transform":
                            garis = f"**{_label_step(step, i)}**"
                            if step.get("target_tool_untuk_konteks"):
                                garis += f" (konteks: `{step['target_tool_untuk_konteks']}`)"
                        else:
                            args_ringkas = []
                            for p, v in (step.get("args") or {}).items():
                                if isinstance(v, dict) and v.get("type") == "from_step":
                                    args_ringkas.append(f"{p}=⟵step{v.get('step')}")
                                elif isinstance(v, dict) and v.get("type") == "manual":
                                    args_ringkas.append(f"{p}={v.get('value')!r}")
                            args_str = ", ".join(args_ringkas) if args_ringkas else "(tanpa argumen)"
                            garis = f"**{i+1}. `{step.get('tool', '?')}`**({args_str})"
                        if i in step_kondisional:
                            garis = "⏭️ *(kondisional -- bisa dilewati)* " + garis
                        if stop_if:
                            garis += f"  \n   ⏹️ *hentikan alur kalau hasil step ini mengandung: \"{stop_if}\"*"
                        lompat = step.get("lompat_jika")
                        if lompat:
                            garis += f"  \n   ↪️ *loncat ke step {lompat['ke_step']+1} kalau hasil mengandung: \"{lompat['mengandung']}\"*"
                        st.markdown(garis)

                st.write("")

                # --- Tombol Aksi ---
                col_aksi1, col_aksi2, col_aksi3, col_aksi4 = st.columns([1, 1, 1, 2])
                with col_aksi1:
                    if auto_detail['status'] == "STOPPED":
                        if st.button("▶️ Start", key=f"start_{selected_id}", use_container_width=True):
                            db.ubah_status(selected_id, "RUNNING")
                            st.rerun()
                    else:
                        if st.button("⏸️ Stop", key=f"stop_{selected_id}", use_container_width=True):
                            db.ubah_status(selected_id, "STOPPED")
                            st.rerun()
                with col_aksi2:
                    #  Edit -- load steps automation ini ke draft, form
                    # "Buat Automation Baru" di bawah otomatis pindah ke mode edit.
                    if st.button("✏️ Edit", key=f"edit_{selected_id}", use_container_width=True):
                        langkah_edit = json.loads(auto_detail['steps_json'])
                        if langkah_edit and isinstance(langkah_edit[0], str):
                            st.error("Automation format LAMA (tanpa argumen) -- gak bisa di-edit, buat ulang dari awal.")
                        else:
                            st.session_state.editing_id = selected_id
                            st.session_state.alur_draft = langkah_edit
                            st.session_state.editing_prefill = {
                                "nama_alur": auto_detail["nama_alur"],
                                "tipe_jadwal": auto_detail["tipe_jadwal"],
                                "waktu_eksekusi": auto_detail["waktu_eksekusi"],
                                "depends_on_id": auto_detail.get("depends_on_id"),
                                "depends_on_status": auto_detail.get("depends_on_status"),
                            }
                            st.rerun()
                with col_aksi3:
                    if st.button("🗑️ Hapus", key=f"del_{selected_id}", type="primary", use_container_width=True):
                        db.hapus_automation(selected_id)
                        st.toast("Automation dihapus!", icon="🗑️")
                        st.rerun()
                with col_aksi4:
                    if st.button("⚡ Jalankan Sekarang", key=f"run_{selected_id}", use_container_width=True):
                        try:
                            r = requests.post(f"{DAEMON_URL}/run_now/{selected_id}", timeout=3)
                            if r.status_code == 200:
                                st.toast("Dipicu! Cek riwayat di bawah beberapa saat lagi.", icon="⚡")
                            else:
                                st.error(f"Daemon menolak: {r.text}")
                        except requests.exceptions.RequestException as e:
                            st.error(f"Gak bisa hubungi daemon: {e}")

                # ---  Riwayat Eksekusi ---
                st.write("---")
                st.write("**🕘 Riwayat Eksekusi Terakhir**")
                riwayat = db.ambil_riwayat(selected_id, limit=5)
                if not riwayat:
                    st.caption("Belum pernah dijalankan.")
                else:
                    for r in riwayat:
                        ikon = {"SUKSES": "✅", "GAGAL": "❌", "DIHENTIKAN": "⏹️"}.get(r["status"], "❔")
                        waktu = datetime.datetime.fromtimestamp(r["mulai_at"]).strftime("%d/%m/%Y %H:%M:%S")
                        with st.expander(f"{ikon} {waktu} — {r['status']}"):
                            try:
                                detail = json.loads(r["detail_json"]) if r["detail_json"] else []
                                st.json(detail)
                            except Exception:
                                st.text(r["detail_json"])

    st.divider()

    # ==========================================
    # BAGIAN B: BUAT / EDIT AUTOMATION
    # ==========================================
    mode_edit = st.session_state.get("editing_id") is not None
    prefill = st.session_state.get("editing_prefill", {}) if mode_edit else {}

    if mode_edit:
        st.subheader(f"✏️ Edit Automation: {prefill.get('nama_alur', '')}")
        if st.button("❌ Batal Edit", key="batal_edit"):
            st.session_state.editing_id = None
            st.session_state.editing_prefill = {}
            st.session_state.alur_draft = []
            st.rerun()
    else:
        st.subheader("🛠️ Buat Automation Baru")

    if "alur_draft" not in st.session_state:
        st.session_state.alur_draft = []  # list of dict step, lihat automation_db.tambah_automation utk formatnya

    key_suffix = f"_edit_{st.session_state.get('editing_id')}" if mode_edit else "_new"

    nama_alur = st.text_input(
        "Nama Automation:", value=prefill.get("nama_alur", ""),
        placeholder="Misal: Reminder Interview Harian", key=f"nama_alur{key_suffix}",
    )
    #  EVENT -- automation ini gak jalan berdasar jam/interval, tapi
    # nunggu automation LAIN selesai dengan status tertentu (lihat diagram
    # chaining yang dibahas sebelumnya).
    opsi_jadwal = ["DAILY", "INTERVAL", "EVENT"]
    default_jadwal_idx = opsi_jadwal.index(prefill["tipe_jadwal"]) if prefill.get("tipe_jadwal") in opsi_jadwal else 0
    tipe_jadwal = st.selectbox("Tipe Jadwal:", opsi_jadwal, index=default_jadwal_idx, key=f"tipe_jadwal{key_suffix}")

    depends_on_id = None
    depends_on_status = None

    if tipe_jadwal == "INTERVAL":
        st.write("**Atur Interval Waktu:**")
        default_val, default_unit = (1, "Detik")
        if prefill.get("tipe_jadwal") == "INTERVAL" and prefill.get("waktu_eksekusi", "").isdigit():
            default_val, default_unit = _pecah_interval(int(prefill["waktu_eksekusi"]))
        sub_col1, sub_col2 = st.columns(2)
        with sub_col1:
            interval_val = st.number_input("Nilai:", min_value=1, value=default_val, key=f"interval_val{key_suffix}")
        with sub_col2:
            interval_unit = st.selectbox(
                "Satuan:", ["Detik", "Menit", "Jam", "Hari"],
                index=["Detik", "Menit", "Jam", "Hari"].index(default_unit), key=f"interval_unit{key_suffix}",
            )
    elif tipe_jadwal == "DAILY":
        st.write("**Atur Jam Eksekusi:**")
        default_jam = datetime.time(9, 0)
        if prefill.get("tipe_jadwal") == "DAILY" and prefill.get("waktu_eksekusi"):
            try:
                default_jam = datetime.datetime.strptime(prefill["waktu_eksekusi"], "%H:%M").time()
            except ValueError:
                pass
        waktu_eksekusi_ui = st.time_input("Jam:", value=default_jam, key=f"waktu_ui{key_suffix}")
    elif tipe_jadwal == "EVENT":
        st.write("**Atur Pemicu (dari automation lain):**")
        # Kalau lagi edit, automation ini sendiri harus dikeluarin dari daftar
        # pilihan pemicu -- gak masuk akal automation nunggu dirinya sendiri.
        daftar_auto_lain = [a for a in db.ambil_semua_automation() if a["id"] != st.session_state.get("editing_id")]
        if not daftar_auto_lain:
            st.warning("Belum ada automation lain yang bisa dijadikan pemicu -- buat automation dasarnya dulu.")
        else:
            peta_nama = {f"{a['nama_alur']}": a['id'] for a in daftar_auto_lain}
            nama_list = list(peta_nama.keys())
            default_dep_idx = 0
            if prefill.get("depends_on_id"):
                nama_dep_lama = next((n for n, i in peta_nama.items() if i == prefill["depends_on_id"]), None)
                if nama_dep_lama:
                    default_dep_idx = nama_list.index(nama_dep_lama)
            pilihan_dep = st.selectbox("Jalankan SETELAH automation ini selesai:", nama_list, index=default_dep_idx, key=f"dep_id{key_suffix}")
            depends_on_id = peta_nama[pilihan_dep]
            opsi_status_dep = ["SUKSES", "GAGAL", "DIHENTIKAN", "ANY"]
            default_status_idx = opsi_status_dep.index(prefill["depends_on_status"]) if prefill.get("depends_on_status") in opsi_status_dep else 0
            depends_on_status = st.selectbox(
                "...dengan status:", opsi_status_dep, index=default_status_idx, key=f"dep_status{key_suffix}",
                help="ANY = jalan berapapun hasil automation pemicu, asal dia baru saja selesai jalan.",
            )

    st.divider()

    # ==========================================
    # 3. RAKIT ALUR EKSEKUSI --  baca argumen tool + pilihan sumber nilai
    #    +  jenis step "AI Transform" (ekstrak nilai dari step lain)
    # ==========================================
    st.write("**Rakit Alur Eksekusi:**")

    jenis_step = st.radio(
        "Jenis Step:",
        ["Panggil Tool", "AI Transform (ekstrak dari step sebelumnya)"],
        key="jenis_step_baru",
        horizontal=True,
        help="AI Transform dipakai kalau output step sebelumnya berupa teks bebas/panjang, "
             "dan tool berikutnya butuh SATU nilai spesifik yang diekstrak dari situ.",
    )

    args_step_ini = {}
    pilihan_tool = None
    ai_sumber_step = None
    ai_target_tool = None
    ai_instruksi = None
    ai_param_terpilih = None    #  param mana di tool tujuan yang diisi hasil AI
    ai_args_lain = {}           #  param LAIN di tool tujuan (kalau lebih dari 1)

    if jenis_step == "Panggil Tool":
        daftar_tools = sorted(t.name for t in ToolRegistry.get_tools("safe"))
        pilihan_tool = st.selectbox("Pilih Tool:", daftar_tools, key="pilihan_tool_baru")

        tool_obj = _ambil_tool_by_name(pilihan_tool)
        skema = _skema_param(tool_obj)

        if skema:
            st.caption(f"Tool `{pilihan_tool}` butuh {len(skema)} argumen:")
            for nama_param, tipe in skema.items():
                sumber = st.radio(
                    f"Sumber nilai untuk `{nama_param}`:",
                    ["Isi Manual", "Dari Output Step Sebelumnya"] if st.session_state.alur_draft else ["Isi Manual"],
                    key=f"sumber_{nama_param}",
                    horizontal=True,
                )
                if sumber == "Isi Manual":
                    nilai = _render_input_manual(nama_param, tipe, key=f"manual_{nama_param}")
                    args_step_ini[nama_param] = {"type": "manual", "value": nilai}
                else:
                    opsi_step = [_label_step(s, i) for i, s in enumerate(st.session_state.alur_draft)]
                    pilihan_step = st.selectbox(
                        f"Ambil `{nama_param}` dari:", opsi_step, key=f"fromstep_{nama_param}"
                    )
                    idx_step = opsi_step.index(pilihan_step)
                    args_step_ini[nama_param] = {"type": "from_step", "step": idx_step}
        else:
            st.caption(f"Tool `{pilihan_tool}` tidak butuh argumen.")

    else:  # AI Transform
        if not st.session_state.alur_draft:
            st.warning("Butuh minimal 1 step lain dulu sebelum bisa nambah AI Transform (dia butuh sumber output).")
        else:
            opsi_step = [_label_step(s, i) for i, s in enumerate(st.session_state.alur_draft)]
            pilihan_sumber = st.selectbox("Ambil teks sumber dari:", opsi_step, key="ai_sumber_step")
            ai_sumber_step = opsi_step.index(pilihan_sumber)

            daftar_tools_konteks = ["(tidak ada -- cuma ekstrak, gak lanjut manggil tool)"] + sorted(t.name for t in ToolRegistry.get_tools("safe"))
            pilihan_target = st.selectbox(
                "Lanjutkan otomatis ke tool (opsional):",
                daftar_tools_konteks, key="ai_target_tool",
                help=" Pilih tool di sini kalau kamu mau hasil ekstraksi AI LANGSUNG dipakai buat "
                     "manggil tool itu -- sistem otomatis nambahin step pemanggilannya, gak perlu kamu "
                     "tambah manual lagi. Kosongkan kalau cuma butuh nilai ekstraksinya buat step LAIN nanti.",
            )
            ai_target_tool = None if pilihan_target.startswith("(tidak ada") else pilihan_target

            ai_instruksi = st.text_area(
                "Instruksi tambahan (opsional):",
                key="ai_instruksi",
                placeholder='Misal: "Ambil SSID wifi bernama Redmi dari daftar jaringan ini."',
                help="Kalau kamu pilih tool tujuan di atas, ini opsional (LLM sudah baca docstring-nya). "
                     "Kalau gak pilih tool tujuan, sebaiknya diisi biar ekstraksinya presisi.",
            )

            if ai_target_tool:
                target_obj = _ambil_tool_by_name(ai_target_tool)
                skema_target = _skema_param(target_obj)
                if not skema_target:
                    st.caption(f"Tool `{ai_target_tool}` tidak butuh argumen -- akan dipanggil langsung tanpa argumen.")
                elif len(skema_target) == 1:
                    ai_param_terpilih = next(iter(skema_target))
                    st.caption(f"Hasil ekstraksi AI akan diisi ke satu-satunya argumen tool ini: `{ai_param_terpilih}`.")
                else:
                    ai_param_terpilih = st.selectbox(
                        f"Argumen `{ai_target_tool}` mana yang diisi hasil ekstraksi AI:",
                        list(skema_target.keys()), key="ai_param_terpilih",
                    )
                    st.caption(f"Argumen `{ai_target_tool}` LAINNYA (diisi terpisah, seperti step tool biasa):")
                    for nama_param, tipe in skema_target.items():
                        if nama_param == ai_param_terpilih:
                            continue
                        sumber_lain = st.radio(
                            f"Sumber nilai untuk `{nama_param}`:",
                            ["Isi Manual", "Dari Output Step Sebelumnya"] if st.session_state.alur_draft else ["Isi Manual"],
                            key=f"ai_lain_sumber_{nama_param}", horizontal=True,
                        )
                        if sumber_lain == "Isi Manual":
                            nilai = _render_input_manual(nama_param, tipe, key=f"ai_lain_manual_{nama_param}")
                            ai_args_lain[nama_param] = {"type": "manual", "value": nilai}
                        else:
                            opsi_step2 = [_label_step(s, i) for i, s in enumerate(st.session_state.alur_draft)]
                            pilihan_step2 = st.selectbox(
                                f"Ambil `{nama_param}` dari:", opsi_step2, key=f"ai_lain_fromstep_{nama_param}"
                            )
                            ai_args_lain[nama_param] = {"type": "from_step", "step": opsi_step2.index(pilihan_step2)}

    stop_if = st.text_input(
        "Hentikan alur kalau hasil step ini mengandung teks tertentu (opsional):",
        key="stop_if_input",
        placeholder='Misal: "TERKONEKSI" -- kosongkan kalau tidak perlu',
        help="Dipakai buat kasus semacam: cek koneksi internet dulu -- kalau HASILNYA sudah menunjukkan "
             "terkoneksi, sisa step di bawahnya (connect wifi dst) TIDAK USAH dijalankan lagi. Kalau AI "
             "Transform ini lanjut otomatis manggil tool, syarat ini dicek di hasil TOOL-nya (step terakhir).",
    )

    if st.button("➕ Tambah ke Alur", use_container_width=True):
        if jenis_step == "Panggil Tool":
            st.session_state.alur_draft.append({
                "type": "tool",
                "tool": pilihan_tool,
                "args": args_step_ini,
                "stop_if_output_contains": stop_if.strip() or None,
            })
            st.rerun()
        elif ai_sumber_step is not None:  # AI Transform, dan sumber step tersedia
            idx_ai = len(st.session_state.alur_draft)  # index step AI Transform SEBELUM di-append
            st.session_state.alur_draft.append({
                "type": "ai_transform",
                "sumber_step": ai_sumber_step,
                "target_tool_untuk_konteks": ai_target_tool,

                "target_param_untuk_konteks": ai_param_terpilih,
                "instruksi": (ai_instruksi or "").strip() or None,

                "stop_if_output_contains": None if ai_target_tool else (stop_if.strip() or None),
            })
            if ai_target_tool:

                args_tool_tujuan = dict(ai_args_lain)
                if ai_param_terpilih:
                    args_tool_tujuan[ai_param_terpilih] = {"type": "from_step", "step": idx_ai}
                st.session_state.alur_draft.append({
                    "type": "tool",
                    "tool": ai_target_tool,
                    "args": args_tool_tujuan,
                    "stop_if_output_contains": stop_if.strip() or None,
                })
            st.rerun()

    # 4. PREVIEW ALUR + REORDER (▲▼) & TOMBOL AKSI
    if st.session_state.alur_draft:
        st.write("")
        _tampilkan_ringkasan_kondisional(st.session_state.alur_draft)
        step_kondisional = _hitung_step_kondisional(st.session_state.alur_draft)
        st.caption("Urutan menentukan step mana yang bisa jadi sumber 'Dari Output Step Sebelumnya' -- geser pakai ▲▼.")
        for i, step in enumerate(st.session_state.alur_draft):
            if step.get("type") == "ai_transform":
                garis = f"**{_label_step(step, i)}**"
                if step.get("target_tool_untuk_konteks"):
                    garis += f" (konteks: `{step['target_tool_untuk_konteks']}`)"
            else:
                args_ringkas = []
                for p, v in step.get("args", {}).items():
                    if v["type"] == "from_step":
                        args_ringkas.append(f"{p}=⟵step{v['step']+1}")
                    else:
                        args_ringkas.append(f"{p}={v['value']!r}")
                args_str = ", ".join(args_ringkas) if args_ringkas else "(tanpa argumen)"
                garis = f"**{i+1}.** `{step.get('tool', '?')}`({args_str})"
            if i in step_kondisional:
                garis = "⏭️ *(kondisional -- bisa dilewati)* " + garis
            if step.get("stop_if_output_contains"):
                garis += f" ⏹️ stop jika mengandung \"{step['stop_if_output_contains']}\""
            if step.get("lompat_jika"):
                lj = step["lompat_jika"]
                garis += f" ↪️ loncat ke step {lj['ke_step']+1} jika mengandung \"{lj['mengandung']}\""

            col_teks, col_up, col_down, col_del = st.columns([7, 1, 1, 1])
            with col_teks:
                st.markdown(garis)
            with col_up:
                if st.button("▲", key=f"up_{i}", disabled=(i == 0), use_container_width=True):
                    if _reorder_aman(st.session_state.alur_draft, i - 1, i):
                        st.session_state.alur_draft[i - 1], st.session_state.alur_draft[i] = \
                            st.session_state.alur_draft[i], st.session_state.alur_draft[i - 1]
                        st.rerun()
                    else:
                        st.error("Gak bisa dipindah -- ada step lain yang argumennya/lompatannya nyangkut ke step ini.")
            with col_down:
                if st.button("▼", key=f"down_{i}", disabled=(i == len(st.session_state.alur_draft) - 1), use_container_width=True):
                    if _reorder_aman(st.session_state.alur_draft, i, i + 1):
                        st.session_state.alur_draft[i], st.session_state.alur_draft[i + 1] = \
                            st.session_state.alur_draft[i + 1], st.session_state.alur_draft[i]
                        st.rerun()
                    else:
                        st.error("Gak bisa dipindah -- step ini butuh argumen dari step di atasnya, atau ada lompatan yang nyangkut ke rentang ini.")
            with col_del:

                if st.button("🗑️", key=f"del_step_{i}", use_container_width=True, help="Hapus step ini"):
                    dipakai_oleh = []
                    for j, s2 in enumerate(st.session_state.alur_draft):
                        if j == i:
                            continue
                        if s2.get("type") == "ai_transform" and s2.get("sumber_step") == i:
                            dipakai_oleh.append(j)
                        for v in (s2.get("args") or {}).values():
                            if isinstance(v, dict) and v.get("type") == "from_step" and v["step"] == i:
                                dipakai_oleh.append(j)
                        lj = s2.get("lompat_jika")
                        if lj and lj.get("ke_step") == i:
                            dipakai_oleh.append(j)
                    if dipakai_oleh:
                        label_dipakai = ", ".join(f"step {j+1}" for j in sorted(set(dipakai_oleh)))
                        st.error(f"Gak bisa dihapus -- dipakai oleh {label_dipakai}. Hapus/ubah itu dulu.")
                    else:
                        st.session_state.alur_draft.pop(i)
                        # Semua referensi (from_step/sumber_step/lompat_jika.ke_step)
                        # yang index-nya LEBIH BESAR dari i ikut digeser -1, biar
                        # tetap nunjuk ke step yang sama walau posisinya geser.
                        for s2 in st.session_state.alur_draft:
                            if s2.get("type") == "ai_transform" and s2.get("sumber_step", -1) > i:
                                s2["sumber_step"] -= 1
                            for v in (s2.get("args") or {}).values():
                                if isinstance(v, dict) and v.get("type") == "from_step" and v["step"] > i:
                                    v["step"] -= 1
                            lj = s2.get("lompat_jika")
                            if lj and lj.get("ke_step", -1) > i:
                                lj["ke_step"] -= 1
                        st.rerun()

            lompat_existing = step.get("lompat_jika")
            stop_if_existing = step.get("stop_if_output_contains")
            opsi_tujuan_steps = [j for j in range(len(st.session_state.alur_draft)) if j > i]
            with st.expander(
                f"⚙️ Syarat step {i+1}"
                + (" -- ADA STOP" if stop_if_existing else "")
                + (" -- ADA LOMPAT" if lompat_existing else "")
            ):
                if stop_if_existing and lompat_existing:
                    st.warning(
                        "Step ini punya DUA syarat sekaligus -- 'stop' SELALU dicek duluan, "
                        "jadi 'loncat' di bawah TIDAK AKAN PERNAH kepakai selama 'stop' masih ada."
                    )

                stop_if_baru = st.text_input(
                    "Hentikan SELURUH alur kalau hasil step ini mengandung:",
                    value=stop_if_existing or "",
                    key=f"stopif_edit_{i}",
                    placeholder='Kosongkan kalau gak perlu',
                )
                col_stop_simpan, col_stop_hapus = st.columns(2)
                with col_stop_simpan:
                    if st.button("💾 Simpan Syarat Stop", key=f"stopif_simpan_{i}", use_container_width=True):
                        st.session_state.alur_draft[i]["stop_if_output_contains"] = stop_if_baru.strip() or None
                        st.rerun()
                with col_stop_hapus:
                    if stop_if_existing and st.button("🗑️ Hapus Syarat Stop", key=f"stopif_hapus_{i}", use_container_width=True):
                        st.session_state.alur_draft[i]["stop_if_output_contains"] = None
                        st.rerun()

                st.divider()

                if not opsi_tujuan_steps:
                    st.caption("Belum ada step SETELAH ini yang bisa jadi tujuan loncatan -- tambah step lain dulu di bawah.")
                else:
                    mengandung = st.text_input(
                        "Loncat MAJU kalau hasil step ini mengandung:",
                        value=(lompat_existing or {}).get("mengandung", ""),
                        key=f"lompat_mengandung_{i}",
                        placeholder='Misal: "TERKONEKSI"',
                    )
                    opsi_label = [_label_step(st.session_state.alur_draft[j], j) for j in opsi_tujuan_steps]
                    default_idx = 0
                    if lompat_existing and lompat_existing.get("ke_step") in opsi_tujuan_steps:
                        default_idx = opsi_tujuan_steps.index(lompat_existing["ke_step"])
                    pilihan_tujuan = st.selectbox("Loncat ke:", opsi_label, index=default_idx, key=f"lompat_tujuan_{i}")
                    idx_tujuan = opsi_tujuan_steps[opsi_label.index(pilihan_tujuan)]

                    col_simpan, col_hapus = st.columns(2)
                    with col_simpan:
                        if st.button("💾 Simpan Percabangan", key=f"lompat_simpan_{i}", use_container_width=True):
                            if mengandung.strip():
                                st.session_state.alur_draft[i]["lompat_jika"] = {
                                    "mengandung": mengandung.strip(), "ke_step": idx_tujuan
                                }
                                st.rerun()
                            else:
                                st.warning("Isi dulu teks syaratnya.")
                    with col_hapus:
                        if lompat_existing and st.button("🗑️ Hapus Percabangan", key=f"lompat_hapus_{i}", use_container_width=True):
                            st.session_state.alur_draft[i].pop("lompat_jika", None)
                            st.rerun()

        if st.button("❌ Reset Alur", use_container_width=True):
            st.session_state.alur_draft = []
            st.rerun()

    st.write("")

    # 5. PENYIMPANAN
    if st.session_state.alur_draft:
        label_tombol = "💾 Update Automation" if mode_edit else "💾 Simpan Automation Baru"
        if st.button(label_tombol, type="primary", use_container_width=True):
            if not nama_alur:
                st.error("Nama Automation tidak boleh kosong!")
            else:

                waktu_eksekusi_final = ""
                if tipe_jadwal == "INTERVAL":
                    multiplier = {"Detik": 1, "Menit": 60, "Jam": 3600, "Hari": 86400}
                    total_detik = interval_val * multiplier[interval_unit]
                    waktu_eksekusi_final = str(total_detik)
                elif tipe_jadwal == "DAILY":
                    waktu_eksekusi_final = waktu_eksekusi_ui.strftime("%H:%M")
                else:  # EVENT -- gak dipakai daemon, lihat automation_db.tambah_automation
                    waktu_eksekusi_final = "-"

                if tipe_jadwal == "EVENT" and not depends_on_id:
                    st.error("Pilih automation pemicu dulu buat tipe EVENT.")
                else:

                    if mode_edit:
                        db.update_automation(
                            alur_id=st.session_state.editing_id,
                            nama_alur=nama_alur,
                            tipe_jadwal=tipe_jadwal,
                            waktu_eksekusi=waktu_eksekusi_final,
                            steps=st.session_state.alur_draft,
                            depends_on_id=depends_on_id,
                            depends_on_status=depends_on_status,
                        )
                        st.session_state.editing_id = None
                        st.session_state.editing_prefill = {}
                    else:
                        db.tambah_automation(
                            nama_alur=nama_alur,
                            tipe_jadwal=tipe_jadwal,
                            waktu_eksekusi=waktu_eksekusi_final,
                            steps=st.session_state.alur_draft,
                            depends_on_id=depends_on_id,
                            depends_on_status=depends_on_status,
                        )

                    st.session_state.alur_draft = []
                    st.toast("Automation di-update!" if mode_edit else "Automation berhasil disimpan!", icon="✅")
                    st.rerun()