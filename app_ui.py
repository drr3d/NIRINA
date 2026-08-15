import streamlit as st
import uuid

# --- IMPORT MODUL KUSTOM ---
from views import(tab1_aichat, tab2_knowledge)
from database.knowledge_db import init_knowledge_db
from core_agent.config import sqlite_db_path, app_dir

# --- KONFIGURASI HALAMAN ---
st.set_page_config(page_title="N.I.R.I.N.A", page_icon="🤖", layout="wide")
st.markdown("<style>.stChatMessage { padding-bottom: 20px; }</style>", unsafe_allow_html=True)
st.title("🤖 Code Workspace")

# ==========================================
# --- SIDEBAR: SIMULASI LOGIN & HAK AKSES ---
# ==========================================
st.sidebar.title("🔒 Keamanan & Akun")
st.sidebar.markdown("Simulasi otorisasi lokal di dalam jaringan:")

# 1. FUNGSI CALLBACK (Inti Solusi Isolasi Data)
# Fungsi ini hanya akan berjalan OTOMATIS ketika user menekan Enter atau mengganti dropdown
def update_identity():
    # Ambil nilai terbaru langsung dari memori widget Streamlit
    username_baru = st.session_state.widget_username.strip()
    st.session_state.username = username_baru if username_baru else "Guest"
    st.session_state.user_role = st.session_state.widget_role
    
    # [MITIGASI KONFLIK]: Bersihkan status persetujuan yang masih menggantung dari user sebelumnya
    st.session_state.menunggu_approval = False
    st.session_state.data_approval = None
    

# 2. INISIALISASI IDENTITAS AWAL (Saat aplikasi pertama kali dibuka)
if "username" not in st.session_state:
    st.session_state.username = "Guest"
if "user_role" not in st.session_state:
    st.session_state.user_role = "Staff"

# 3. WIDGET INPUT YANG SUDAH TERIKAT DENGAN CALLBACK
st.sidebar.text_input(
    "Nama Pengguna / ID Staff:", 
    value=st.session_state.username,
    key="widget_username",            # <-- Streamlit akan menyimpan teks kesini
    on_change=update_identity,        # <-- Memicu fungsi di atas saat ditekan Enter
    help="Tekan 'Enter' setelah mengetik nama untuk mengganti akun dan memuat riwayat chat."
)

st.sidebar.selectbox(
    "Pilih Role Anda:", 
    ["Staff", "Admin"],
    index=0 if st.session_state.user_role == "Staff" else 1,
    key="widget_role",                # <-- Streamlit akan menyimpan role kesini
    on_change=update_identity,        # <-- Memicu fungsi saat dropdown diganti
    help="Role 'Admin'."
)

st.sidebar.info(f"Aktif sebagai: **{st.session_state.user_role}** ({st.session_state.username})")

# ==========================================
# --- INISIALISASI SESSION STATE & DB ---
# ==========================================
# [NEW UPGRADE]: Folder untuk menyimpan dokumen fisik HR Knowledge
knowledge_dir = app_dir / "knowledge_docs"
knowledge_dir.mkdir(parents=True, exist_ok=True)

#init_chat_db() # Pastikan tabel DB sudah ada
init_knowledge_db()
#init_interview_db()
#init_lowongan_db()

# 1. Manajemen Daftar Thread (Sesi)
if "daftar_thread" not in st.session_state:
    # Idealnya daftar ini ditarik dari SQLite (misal tabel list_sesi).
    # Untuk contoh ini, kita sediakan 1 sesi default jika kosong.
    st.session_state.daftar_thread = ["Sesi Utama (Default)"] 

if "active_thread_id" not in st.session_state:
    st.session_state.active_thread_id = "Sesi Utama (Default)"

# 2. UI Pemilihan Sesi di Sidebar
st.sidebar.divider()
st.sidebar.markdown("### 💬 Ruang Kerja Chat")

# Tombol Buat Sesi Baru
if st.sidebar.button("➕ Buat Ruang Chat Baru", use_container_width=True):
    # Bikin ID unik namun tetap terbaca (misal: Sesi-a1b2c3d4)
    new_thread = f"Sesi-{str(uuid.uuid4())[:8]}"
    st.session_state.daftar_thread.append(new_thread)
    st.session_state.active_thread_id = new_thread
    
    # Kosongkan UI untuk sesi baru
    st.session_state.messages = [] 
    st.session_state.menunggu_approval = False
    st.rerun()

# Dropdown Pilih Sesi
pilihan_sesi = st.sidebar.selectbox(
    "Lanjutkan Percakapan:",
    st.session_state.daftar_thread,
    index=st.session_state.daftar_thread.index(st.session_state.active_thread_id)
)

# 3. Logika Perpindahan Sesi
# 3. Logika Perpindahan Sesi (Thread)
if pilihan_sesi != st.session_state.active_thread_id:
    st.session_state.active_thread_id = pilihan_sesi
    
    # [MITIGASI KONFLIK]: Pembersihan sisa interaksi saat pindah thread
    st.session_state.menunggu_approval = False
    st.session_state.data_approval = None
    

    st.rerun()

# ==========================================
# --- LAYOUTING TABS ---
# ==========================================
tab1, tab2, = st.tabs([
    "💬 AI Assistant",
    "📚 Knowledge Base",
])

with tab1:
    tab1_aichat.render()

with tab2:
    tab2_knowledge.render(sqlite_db_path, knowledge_dir)