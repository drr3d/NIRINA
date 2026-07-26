import streamlit as st
import sqlite3
import pandas as pd

#  ------------------------------------------
# TAB 2: DATABASE KANDIDAT (INTERAKTIF)
# ------------------------------------------
def render(sqlite_db_path):
    st.markdown("### 🗂️ Katalog Kandidat (Interaktif & Real-time)")
    
    if sqlite_db_path.exists():
        try:
            with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
                # 1. PASANG PRAGMA WAL DI SINI (Baris pertama operasi)
                conn.execute("PRAGMA journal_mode=WAL;")
                df = pd.read_sql_query("SELECT * FROM kandidat", conn)
            
            if not df.empty:
                col1, col2, col3 = st.columns(3)
                col1.metric("Total Kandidat", len(df))
                
                if "usia" in df.columns:
                    mean_usia = df[df["usia"] > 0]["usia"].mean()
                    col2.metric("Rata-rata Usia", round(mean_usia, 1) if pd.notna(mean_usia) else 0)
                    
                if "lama_bekerja_tahun" in df.columns:
                    mean_pengalaman = df["lama_bekerja_tahun"].mean()
                    col3.metric("Rata-rata Pengalaman", f"{round(mean_pengalaman, 1)} Thn")
                
                column_config = {
                    "id": st.column_config.NumberColumn("ID", disabled=True), 
                    "file_cv": st.column_config.TextColumn("File CV", disabled=True), 
                    "gender": st.column_config.SelectboxColumn(
                        "Gender", help="Pilih jenis kelamin", options=["Pria", "Wanita"], required=False
                    ),
                    "pekerjaan_aktif_saat_ini": st.column_config.TextColumn("Pekerjaan Aktif", disabled=True),
                    "skill_utama": st.column_config.TextColumn("Skill Utama", disabled=True)
                }

                st.markdown("💡 *Anda dapat mengedit data langsung pada tabel di bawah ini (klik dua kali pada sel).*")
                
                edited_df = st.data_editor(
                    df, use_container_width=True, column_config=column_config,
                    hide_index=True, num_rows="fixed"
                )
                
                if st.button("💾 Simpan Perubahan ke Database", type="primary"):
                    try:
                        with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
                            cursor = conn.cursor()
                            # AKTIFKAN MODE WAL (Write-Ahead Logging) AGAR MULTI-PROCESS AMAN
                            cursor.execute("PRAGMA journal_mode=WAL;")
                            for index, row in edited_df.iterrows():
                                cursor.execute('''
                                    UPDATE kandidat 
                                    SET nama_kandidat=?, gender=?, usia=?, asal_daerah=?,
                                        status_kalkulasi=?, pendidikan_terakhir=?, jurusan=?, ipk=?, 
                                        lama_bekerja_tahun=?
                                    WHERE id=?
                                ''', (
                                    row['nama_kandidat'], row['gender'], row['usia'], row['asal_daerah'],
                                    row['status_kalkulasi'], row['pendidikan_terakhir'], row['jurusan'], row['ipk'],
                                    row['lama_bekerja_tahun'], row['id']
                                ))
                            conn.commit()
                        st.success("✅ Perubahan data berhasil disimpan!")
                    except Exception as e:
                        st.error(f"❌ Gagal menyimpan data: {e}")
                        
            else:
                st.warning("Database SQLite sudah dibuat, tetapi belum ada data kandidat.")
        except Exception as e:
            st.error(f"Gagal membaca atau memproses database SQLite: {e}")
    else:
        st.info("Sistem menunggu database SQLite dibuat.")