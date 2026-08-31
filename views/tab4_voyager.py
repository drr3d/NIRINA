import streamlit as st
import numpy as np
from pathlib import Path

from core_agent.agent_factory.voyager.voyager_vizcore import (
    ambil_data_skill,
    buat_embedding_fn,
    cluster_task_desc,
    buat_figure_scatter,
    bangun_graph_tool,
    buat_html_network,
    filter_edges_by_weight,
    hitung_jumlah_jembatan,
)
 
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = str(BASE_DIR / "skill_library_db")
DEFAULT_COLLECTION = "agent_skills"
 
 
@st.cache_data(show_spinner=False)
def _proses_data(db_path: str, collection_name: str, min_cluster_size: int,
                  embed_backend: str, ollama_base_url: str, ollama_model: str, st_model_name: str):
    embed_fn = buat_embedding_fn(
        backend=embed_backend, ollama_base_url=ollama_base_url,
        ollama_model=ollama_model, st_model_name=st_model_name,
    )
    ids, docs, metas, embeddings = ambil_data_skill(db_path, collection_name, embedding_fn=embed_fn)

    koordinat_2d, label_cluster, catatan_cluster = cluster_task_desc(embeddings, min_cluster_size)
    return docs, metas, koordinat_2d, label_cluster, catatan_cluster
 
 
def render():
    st.subheader("🧭 Voyager Trace Visualizer")
    st.caption(
        "Peta cluster task (dari task_desc) + jaringan urutan tool call. "
        "Klik node/edge di graph buat lihat contoh task-nya."
    )
 
    with st.expander("⚙️ Pengaturan sumber data", expanded=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            db_path = st.text_input("Path skill_library_db", value=DEFAULT_DB_PATH)
        with col2:
            collection_name = st.text_input("Nama collection", value=DEFAULT_COLLECTION)
        with col3:
            min_cluster_size = st.number_input("min_cluster_size (HDBSCAN)", min_value=2, max_value=20, value=3)
 
        st.caption(
            "Embedding di-hitung ULANG di sini (bukan baca dari Chroma) -- "
            "samain dengan setting SkillLibrary() kamu di agent_factory.py."
        )
        ec1, ec2, ec3 = st.columns(3)
        with ec1:
            embed_backend = st.selectbox("Embedding backend", ["ollama", "st"], index=0)
        with ec2:
            ollama_base_url = st.text_input("Ollama base URL", value="http://localhost:11434",
                                             disabled=(embed_backend != "ollama"))
        with ec3:
            if embed_backend == "ollama":
                ollama_model = st.text_input("Ollama model", value="nomic-embed-text")
                st_model_name = "all-MiniLM-L6-v2"
            else:
                st_model_name = st.text_input("Sentence-Transformers model", value="all-MiniLM-L6-v2")
                ollama_model = "nomic-embed-text"
 
    if st.button("🔄 Refresh Visualisasi"):
        _proses_data.clear()
 
    try:
        with st.spinner("Mengambil data & menghitung ulang embedding + cluster..."):
            docs, metas, koordinat_2d, label_cluster, catatan_cluster = _proses_data(
                db_path, collection_name, min_cluster_size,
                embed_backend, ollama_base_url, ollama_model, st_model_name,
            )
    except RuntimeError as e:

        st.warning(str(e))
        return
    except Exception as e:

        st.error(f"Gagal memuat skill library: {e}")
        return
 
    if len(docs) == 0:
        st.info("Skill library masih kosong -- belum ada trace buat divisualisasikan.")
        return

    if catatan_cluster:
        st.info(f"ℹ️ {catatan_cluster}")
 
    # ==========================================
    # FILTER
    # ==========================================
    daftar_cluster = sorted(set(int(c) for c in label_cluster))
    label_opsi_cluster = {c: (f"Cluster {c}" if c >= 0 else "Noise (-1)") for c in daftar_cluster}
 
    fc1, fc2, fc3, fc4 = st.columns([2, 1.3, 1.3, 1.4])
    with fc1:
        cluster_terpilih = st.multiselect(
            "Filter cluster", options=daftar_cluster,
            default=daftar_cluster, format_func=lambda c: label_opsi_cluster[c],
        )
    with fc2:
        status_filter = st.radio("Status", ["Semua", "Berhasil", "Gagal"], horizontal=False)
    with fc3:
        min_weight = st.slider("Min. frekuensi edge", min_value=1, max_value=10, value=1)
    with fc4:
        color_mode_label = st.radio("Warnai berdasarkan", ["Cluster", "Success rate"], horizontal=False)
    color_mode = "success_rate" if color_mode_label == "Success rate" else "cluster"
 
    status_target = {"Semua": None, "Berhasil": "berhasil", "Gagal": "gagal"}[status_filter]
 
    mask = np.array([
        (int(c) in cluster_terpilih) and (status_target is None or m.get("status") == status_target)
        for c, m in zip(label_cluster, metas)
    ])
 
    if not mask.any():
        st.info("Tidak ada task yang cocok dengan filter ini.")
        return
 
    docs_f = [d for d, keep in zip(docs, mask) if keep]
    metas_f = [m for m, keep in zip(metas, mask) if keep]
    koord_f = koordinat_2d[mask]
    label_f = label_cluster[mask]
 
    n_cluster = len(set(label_f)) - (1 if -1 in label_f else 0)
    n_noise = int((label_f == -1).sum())
 
    m1, m2, m3 = st.columns(3)
    m1.metric("Task ditampilkan", f"{int(mask.sum())} / {len(docs)}")
    m2.metric("Jumlah cluster", n_cluster)
    m3.metric("Task masuk 'noise'", n_noise)
 
    tab_scatter, tab_network = st.tabs(["📍 Peta Cluster Task", "🕸️ Jaringan Tool"])
 
    with tab_scatter:
        fig = buat_figure_scatter(koord_f, label_f, docs_f, metas_f)
        st.plotly_chart(fig, use_container_width=True)
 
    with tab_network:
        graph_data = bangun_graph_tool(docs_f, metas_f, label_f)
        graph_data = filter_edges_by_weight(graph_data, min_weight)
 
        if not graph_data["edge_cluster"]:
            st.info("Belum ada trace tool call yang bisa digambar jadi graph dengan filter ini.")
        else:
            n_jembatan = hitung_jumlah_jembatan(graph_data)
            st.caption(f"{n_jembatan} edge terdeteksi sebagai jembatan lintas cluster.")
            html_str = buat_html_network(graph_data, color_mode=color_mode)
            st.components.v1.html(html_str, height=800, scrolling=True)