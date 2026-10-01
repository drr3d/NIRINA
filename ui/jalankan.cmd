@echo off
rem Menjalankan dashboard nigate (Streamlit) di http://127.0.0.1:8502
rem Butuh:  pip install -r requirements.txt
rem Set dulu token admin gateway (dan alamatnya bila bukan bawaan):
rem     set NIGATE_ADMIN_TOKEN=isi_token
rem     set NIGATE_ADMIN_URL=http://127.0.0.1:4001
cd /d "%~dp0"
python -m streamlit run app.py --server.port 8502 --server.address 127.0.0.1
