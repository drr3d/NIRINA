import streamlit as st
from core_agent.agent_graph import proses_chat_agent

# ------------------------------------------
# TAB 1: AI ASSISTANT
# ------------------------------------------
def render():
    st.subheader("💬 AI Assistant")
    
    # ==========================================
    # --- INISIALISASI SESSION STATE ---
    # ==========================================
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "menunggu_approval" not in st.session_state:
        st.session_state.menunggu_approval = False
        st.session_state.data_approval = None
    
    if st.button("🗑️ Bersihkan Riwayat Chat"):
        st.session_state.messages = []
        st.session_state.menunggu_approval = False
        st.session_state.data_approval = None
        st.rerun()

    chat_container = st.container(height=500)

    # 1. Render histori chat
    with chat_container:
        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

    # ==========================================
    # 2. LOGIKA CHAT & APPROVAL (HITL) 
    # ==========================================
    if st.session_state.menunggu_approval:
        approval_ui = st.empty()
        with approval_ui.container():
            st.warning("⚠️ **Sistem Membutuhkan Persetujuan Anda**")
            
            data = st.session_state.data_approval
            pesan_peringatan = data.get("pesan", "AI ingin mengeksekusi tool sensitif.") if data else "AI ingin mengeksekusi tool sensitif."
            st.info(pesan_peringatan)
            
            col1, col2 = st.columns(2)
            with col1:
                # [KOREKSI 1] Gunakan width="stretch" sesuai standar Streamlit terbaru
                if st.button("✅ Setujui & Lanjutkan", width="stretch"):
                    approval_ui.empty() 
                    with chat_container:
                        with st.chat_message("assistant"):
                            with st.spinner("AI sedang mengeksekusi tindakan (otomatis meneruskan jika berantai)..."):
                                
                                # [KOREKSI 2] Gunakan is_approval=True sesuai fungsi backend asli Anda
                                hasil = proses_chat_agent(
                                            is_approval=True, 
                                            thread_id=st.session_state.active_thread_id,
                                            user_role=st.session_state.user_role
                                        )
                                
                                # Bypass persetujuan berantai
                                while hasil.get("status") == "butuh_persetujuan":
                                    hasil = proses_chat_agent(
                                                is_approval=True, 
                                                thread_id=st.session_state.active_thread_id,
                                                user_role=st.session_state.user_role
                                            )
                                
                                pesan_final = hasil.get("pesan", "")
                                st.markdown(pesan_final)
                                
                                st.session_state.messages.append({
                                    "role": "assistant", 
                                    "content": pesan_final,
                                    "download_file": hasil.get("download_info")
                                })
                                
                                st.session_state.menunggu_approval = False
                                st.session_state.data_approval = None
                                st.rerun()
            
            with col2:
                # [FIX] Tombol ini WAJIB memanggil backend, bukan cuma reset state
                # Streamlit -- kalau tidak, checkpoint LangGraph di SQLite tetap
                # nyangkut permanen di titik interrupt (node_sensitive/node_pentest),
                # walau tampilan UI-nya sudah "pura-pura" balik normal. Memanggil
                # proses_chat_agent(is_approval=False, ...) memicu logika abort yang
                # sudah ada di AgentSession.run() (agent_graph.py): tool_call yang
                # pending dijawab dengan ToolMessage "SYSTEM ABORT", lalu AI lanjut
                # dengan instruksi baru -- BUKAN mematikan seluruh sesi.
                if st.button("❌ Batalkan", type="primary", width="stretch"):
                    approval_ui.empty()
                    with chat_container:
                        with st.chat_message("assistant"):
                            with st.spinner("Membatalkan aksi & melanjutkan..."):
                                hasil = proses_chat_agent(
                                    is_approval=False,
                                    user_input=(
                                        "[SYSTEM] User membatalkan aksi tool tadi. "
                                        "Jangan ulangi tool yang sama - tanyakan instruksi "
                                        "lanjutan ke user, atau hentikan proses ini kalau "
                                        "memang sudah tidak relevan."
                                    ),
                                    thread_id=st.session_state.active_thread_id,
                                    user_role=st.session_state.user_role
                                )

                            if hasil.get("status") == "butuh_persetujuan":
                                # AI langsung minta approval lagi (mis. coba tool sensitif
                                # lain) -- tampilkan approval box baru, JANGAN dianggap selesai.
                                st.session_state.menunggu_approval = True
                                st.session_state.data_approval = hasil
                            else:
                                pesan_final = hasil.get("pesan", "")
                                st.markdown(pesan_final)
                                st.session_state.messages.append({
                                    "role": "assistant",
                                    "content": pesan_final,
                                    "download_file": hasil.get("download_info")
                                })
                                st.session_state.menunggu_approval = False
                                st.session_state.data_approval = None
                    st.rerun()

    # ==========================================
    # 3. INPUT NORMAL (Hanya aktif jika tidak ada approval)
    # ==========================================
    elif not st.session_state.menunggu_approval:
        if prompt := st.chat_input("Tanya apa saja di sini..."):
            with chat_container:
                with st.chat_message("user"):
                    st.markdown(prompt)
            
            st.session_state.messages.append({"role": "user", "content": prompt})

            with chat_container:
                with st.chat_message("assistant"):
                    with st.spinner("AI sedang menganalisis data..."):
                        hasil = proses_chat_agent(
                            user_input=prompt, 
                            thread_id=st.session_state.active_thread_id,
                            user_role=st.session_state.user_role
                        )
                        
                        if hasil.get("status") == "butuh_persetujuan":
                            st.session_state.menunggu_approval = True
                            st.session_state.data_approval = hasil
                            st.rerun()
                        else:
                            pesan = hasil.get("pesan", "")
                            st.markdown(pesan)
                            st.session_state.messages.append({
                                "role": "assistant", 
                                "content": pesan,
                                "download_file": hasil.get("download_info")
                            })