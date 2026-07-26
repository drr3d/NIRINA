import streamlit as st
from pathlib import Path

from core_agent.agent_graph import proses_chat_agent
from database.chat_db import save_chat_message, clear_chat_history
# ------------------------------------------
# TAB 1: AI ASSISTANT
# ------------------------------------------
def render(temp_dir):
    st.subheader("💬 AI Recruitment Assistant")
    
    if st.button("🗑️ Bersihkan Riwayat Chat"):
        clear_chat_history(
            thread_id=st.session_state.active_thread_id, 
            username=st.session_state.username
        )
        st.session_state.messages = []
        st.session_state.menunggu_approval = False
        st.session_state.data_approval = None
        st.rerun()

    chat_container = st.container(height=500)
    
    # 1. Render Histori Chat
    with chat_container:
        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                
                if "download_file" in message and message["download_file"]:
                    file_info = message["download_file"]
                    file_path = Path(file_info["path"])
                    
                    if st.session_state.user_role == "HR Admin":
                        if file_path.exists():
                            with open(file_path, "rb") as f:
                                st.download_button(
                                    label=f"📥 Unduh CV Asli: {file_info['nama_file']}",
                                    data=f.read(),
                                    file_name=file_info["nama_file"],
                                    mime="application/octet-stream",
                                    key=f"dl_{file_path.name}_{message['content'][:10]}"
                                )
                        else:
                            st.error("📁 Berkas fisik tidak ditemukan di harddisk server.")
                    else:
                        st.error("🔒 Akses Dihentikan: Anda tidak memiliki wewenang mengunduh dokumen asli.")

    # 2. Logika Chat & Approval (HITL)
    if st.session_state.menunggu_approval:
        approval_ui = st.empty()
        with approval_ui.container():
            st.warning("⚠️ **Sistem Membutuhkan Persetujuan Anda**")
            st.info(st.session_state.data_approval["pesan"])
            
            col1, col2 = st.columns(2)
            with col1:
                # Perbaikan Warning Streamlit: ganti use_container_width menjadi width="stretch"
                if st.button("✅ Setujui & Lanjutkan", width="stretch"):
                    approval_ui.empty()
                    with chat_container:
                        with st.chat_message("assistant"):
                            # Teks spinner disesuaikan karena AI mungkin berpikir lebih lama
                            with st.spinner("AI sedang mengeksekusi tindakan (otomatis meneruskan jika berantai)..."):
                                
                                # 1. Eksekusi pertama setelah tombol ditekan
                                hasil = proses_chat_agent(
                                            is_approval=True, 
                                            thread_id=st.session_state.active_thread_id,
                                            user_role=st.session_state.user_role
                                        )
                                
                                # ==========================================
                                # 2. [AUTO-APPROVE LOOP]: Bypass persetujuan berantai
                                # ==========================================
                                # Selama AI masih meminta persetujuan di task yang sama,
                                # sistem akan otomatis mem-bypass dan menekan "Gas Terus" untuk AI.
                                while hasil.get("status") == "butuh_persetujuan":
                                    hasil = proses_chat_agent(
                                                is_approval=True, 
                                                thread_id=st.session_state.active_thread_id,
                                                user_role=st.session_state.user_role
                                            )
                                
                                # ==========================================
                                # 3. Selesai Loop = Mendapat Jawaban Final
                                # ==========================================
                                st.markdown(hasil["pesan"])
                                
                                save_chat_message(
                                    "assistant", 
                                    hasil["pesan"], 
                                    hasil.get("download_info"), 
                                    thread_id=st.session_state.active_thread_id,
                                    username=st.session_state.username
                                )
                                st.session_state.messages.append({
                                    "role": "assistant", 
                                    "content": hasil["pesan"],
                                    "download_file": hasil.get("download_info")
                                })
                                
                                # Bersihkan status persetujuan
                                st.session_state.menunggu_approval = False
                                st.session_state.data_approval = None
                                st.rerun()
                
            with col2:
                if st.button("❌ Batalkan", width="stretch"):
                    st.session_state.menunggu_approval = False
                    st.session_state.data_approval = None
                    st.rerun()

    # --- JIKA TIDAK ADA APPROVAL, TAMPILKAN INPUT CHAT & DRAG DROP ---
    elif not st.session_state.menunggu_approval:
        
        # INISIALISASI KEY UNTUK RESET UPLOADER
        if "file_uploader_key" not in st.session_state:
            st.session_state.file_uploader_key = 0

        # MENGGUNAKAN POPOVER AGAR UPLOADER TERSEMBUNYI (Lebih elegan)
        with st.popover("📎 Lampirkan Dokumen (Opsional)"):
            st.markdown("Hanya menerima file `.txt` dan `.pdf` (Maks: 2MB | 5 Halaman).")
            lampiran_dokumen = st.file_uploader(
                "Pilih File (Draft Lowongan / CV Pelamar)", # [PERBAIKAN 1]: Label UI dinetralkan
                type=["txt", "pdf"],
                accept_multiple_files=False,
                key=f"uploader_{st.session_state.file_uploader_key}", 
                label_visibility="collapsed" 
            )

        prompt = st.chat_input("Tanya seputar kandidat atau minta AI memproses file di atas...")
        
        if prompt:
            teks_prompt_ke_ai = prompt
            info_lampiran_ui = ""
            
            if lampiran_dokumen is not None:
                # ==========================================
                # 🛡️ LAPISAN VALIDASI UPFRONT (SEBELUM PROSES)
                # ==========================================
                ext = lampiran_dokumen.name.split('.')[-1].lower()
                if ext not in ['txt', 'pdf']:
                    st.error("❌ Akses Ditolak: Sistem hanya menerima file berekstensi .txt atau .pdf.")
                    st.stop()
                
                if lampiran_dokumen.size > 2097152:
                    st.error(f"❌ File terlalu besar! Ukuran file Anda: {round(lampiran_dokumen.size/1024/1024, 2)}MB. (Maksimal 2MB)")
                    st.stop()

                # --- TAMBAHAN BARU: SIMPAN KE FOLDER SEMENTARA (STAGING AREA) ---
                lokasi_simpan_sementara = temp_dir / lampiran_dokumen.name
                with open(lokasi_simpan_sementara, "wb") as f:
                    f.write(lampiran_dokumen.getbuffer())

                # ==========================================
                # ⚙️ EKSTRAKSI TEKS
                # ==========================================
                isi_teks = ""
                try:
                    if ext == 'txt':
                        isi_teks = lampiran_dokumen.getvalue().decode('utf-8')
                        
                    elif ext == 'pdf':
                        import PyPDF2 
                        pdf_reader = PyPDF2.PdfReader(lampiran_dokumen)
                        
                        if len(pdf_reader.pages) > 5:
                            st.error(f"❌ Dokumen terlalu panjang! PDF Anda memiliki {len(pdf_reader.pages)} halaman. (Maksimal 5 Halaman)")
                            st.stop()
                            
                        for page in pdf_reader.pages:
                            teks_halaman = page.extract_text()
                            if teks_halaman:
                                isi_teks += teks_halaman + "\n"
                                
                    if not isi_teks.strip():
                        st.error("❌ Dokumen kosong atau teks tidak dapat diekstrak.")
                        st.stop()

                    # [PERBAIKAN 2]: Hapus label (Isi Draft Lowongan) agar AI menebak sendiri isinya
                    teks_prompt_ke_ai += f"\n\n--- DOKUMEN LAMPIRAN: {lampiran_dokumen.name} ---\n{isi_teks}"
                    info_lampiran_ui = f"\n\n> 📎 *Membaca dokumen yang dilampirkan: {lampiran_dokumen.name}*"
                    
                except Exception as e:
                    st.error(f"Gagal memproses file: {e}")
                    st.stop()

            teks_tampil_di_ui = prompt + info_lampiran_ui
            with chat_container:
                with st.chat_message("user"):
                    st.markdown(teks_tampil_di_ui)
            
            # [UPDATE]: Simpan chat user terikat dengan identitasnya
            save_chat_message(
                "user", 
                teks_tampil_di_ui, 
                thread_id=st.session_state.active_thread_id, 
                username=st.session_state.username
            )
            st.session_state.messages.append({"role": "user", "content": teks_tampil_di_ui})

            # === LOGIKA RESET UPLOADER ===
            # Naikkan angka key agar Streamlit merender uploader baru yang kosong
            st.session_state.file_uploader_key += 1

            # === [PERBAIKAN] PESAN SPINNER DINAMIS ===
            pesan_spinner = "AI sedang membaca lampiran dan mengeksekusi..." if lampiran_dokumen is not None else "AI sedang memproses instruksi Anda..."
            with chat_container:
                with st.chat_message("assistant"):
                    with st.spinner(pesan_spinner):
                        hasil = proses_chat_agent(
                                                    user_input=teks_prompt_ke_ai, 
                                                    thread_id=st.session_state.active_thread_id,  # <--- TAMBAHKAN INI
                                                    user_role=st.session_state.user_role
                                                )
                        
                        if hasil["status"] == "butuh_persetujuan":
                            st.session_state.menunggu_approval = True
                            st.session_state.data_approval = hasil
                            st.rerun()
                        else:
                            st.markdown(hasil["pesan"])
                            save_chat_message(
                                "assistant", 
                                hasil["pesan"], 
                                hasil.get("download_info"), 
                                thread_id=st.session_state.active_thread_id,
                                username=st.session_state.username
                            )
                            st.session_state.messages.append({
                                "role": "assistant", 
                                "content": hasil["pesan"],
                                "download_file": hasil.get("download_info")
                            })
                            st.rerun() # Refresh halaman untuk memastikan uploader benar-benar bersih

    elif prompt := st.chat_input("Tanya seputar kandidat di sini..."):
        with chat_container:
            with st.chat_message("user"):
                st.markdown(prompt)
        
        save_chat_message(
            "user", 
            prompt, 
            thread_id=st.session_state.active_thread_id, 
            username=st.session_state.username
        )
        st.session_state.messages.append({"role": "user", "content": prompt})

        with chat_container:
            with st.chat_message("assistant"):
                with st.spinner("AI sedang menganalisis data..."):
                    hasil = proses_chat_agent(
                                                user_input=prompt, 
                                                thread_id=st.session_state.active_thread_id,  # <--- TAMBAHKAN INI
                                                user_role=st.session_state.user_role
                                            )
                    
                    if hasil["status"] == "butuh_persetujuan":
                        st.session_state.menunggu_approval = True
                        st.session_state.data_approval = hasil
                        st.rerun()
                    else:
                        st.markdown(hasil["pesan"])
                        save_chat_message(
                            "assistant", 
                            hasil["pesan"], 
                            hasil.get("download_info"), 
                            thread_id=st.session_state.active_thread_id,
                            username=st.session_state.username
                        )
                        st.session_state.messages.append({
                            "role": "assistant", 
                            "content": hasil["pesan"],
                            "download_file": hasil.get("download_info")
                        })