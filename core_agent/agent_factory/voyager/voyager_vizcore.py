import json
from collections import defaultdict

import numpy as np
import networkx as nx
import plotly.graph_objects as go
from pyvis.network import Network

import chromadb
from chromadb.utils import embedding_functions

# ==========================================
# EMBEDDING BACKEND FACTORY
# (disalin dari skill_lib.py -- dipakai buat RE-EMBED task_desc di sini,
# BUKAN baca embeddings tersimpan dari Chroma. Lihat catatan di
# ambil_data_skill() kenapa.)
# ==========================================
def buat_embedding_fn(
    backend: str = "ollama",
    ollama_base_url: str = "http://localhost:11434",
    ollama_model: str = "nomic-embed-text",
    st_model_name: str = "all-MiniLM-L6-v2",
    custom_fn=None,
):
    if backend == "ollama":
        return embedding_functions.OllamaEmbeddingFunction(
            url=f"{ollama_base_url}/api/embeddings", model_name=ollama_model,
        )
    elif backend == "st":
        return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=st_model_name)
    elif backend == "custom":
        if custom_fn is None:
            raise ValueError("backend='custom' butuh custom_fn (embedding function callable).")
        return custom_fn
    else:
        raise ValueError(f"Backend embedding tidak dikenal: {backend!r} (pilih 'ollama'/'st'/'custom')")

# ==========================================
# WARNA PALET UNTUK CLUSTER
# ==========================================
PALET_WARNA = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#46f0f0", "#f032e6", "#bcf60c", "#008080", "#9a6324",
    "#800000", "#808000", "#000075", "#ff69b4", "#1e90ff",
]
WARNA_NOISE = "#7f7f7f"     # cluster -1 (noise, HDBSCAN gagal masukin ke cluster manapun)
WARNA_JEMBATAN = "#ffd700"  # edge yang dipakai lintas cluster

def warna_untuk_cluster(cid: int) -> str:
    if cid < 0:
        return WARNA_NOISE
    return PALET_WARNA[cid % len(PALET_WARNA)]

def _lerp(a, b, t):
    return a + (b - a) * t

def warna_dari_rasio_sukses(rasio: float) -> str:
    """Interpolasi merah (0% berhasil) -> kuning (50%) -> hijau (100%)."""
    r = max(0.0, min(1.0, rasio))
    stops = [(0.0, (231, 76, 60)), (0.5, (241, 196, 15)), (1.0, (46, 204, 113))]
    for (r0, c0), (r1, c1) in zip(stops, stops[1:]):
        if r0 <= r <= r1:
            t = 0 if r1 == r0 else (r - r0) / (r1 - r0)
            rC, gC, bC = (int(_lerp(c0[i], c1[i], t)) for i in range(3))
            return f"#{rC:02x}{gC:02x}{bC:02x}"
    return "#999999"

def rasio_sukses(status_count: dict) -> float:
    total = sum(status_count.values())
    if total == 0:
        return 0.5  # netral (kuning) kalau nggak ada data
    return status_count.get("berhasil", 0) / total

# ==========================================
# 1. AMBIL DATA DARI CHROMADB
# ==========================================
def ambil_data_skill(db_path: str, collection_name: str, embedding_fn=None):
    """
    SENGAJA cuma include=["documents","metadatas"] -- PERSIS kayak
    skilllib_viewer.py kamu. Minta "embeddings" balik dari Chroma itu yang
    memicu Chroma nyoba baca vector dari index HNSW biner berdasarkan id --
    kalau ada 1 aja id yang vector-nya "orphan"/rusak di index itu (biasanya
    dari penulisan yang sempat ke-interrupt), Chroma lempar
    "Internal error: Error finding id" dan GAGAL TOTAL, bukan skip id itu
    doang. skilllib_viewer.py nggak pernah kena ini karena dia emang nggak
    pernah minta embeddings.

    Solusinya: re-embed task_desc di sini pakai embedding_fn yang sama
    (default: backend Ollama sama kayak skill_lib.py), bukan gantungin ke
    embeddings yang tersimpan. Efeknya butuh sedikit waktu ekstra buat
    embed ulang, tapi nggak nyentuh titik yang error sama sekali.
    """
    client = chromadb.PersistentClient(path=db_path)
    col = client.get_or_create_collection(name=collection_name)
    hasil = col.get(include=["documents", "metadatas"])

    ids = hasil["ids"]
    docs = hasil["documents"]
    metas = hasil["metadatas"]

    if not docs:
        raise RuntimeError(
            "Collection kosong. Pastikan db_path/collection_name sudah benar "
            "(samain kayak di skilllib_viewer.py kamu)."
        )

    embed_fn = embedding_fn or buat_embedding_fn(backend="ollama")
    embeddings = np.array(embed_fn(docs))

    return ids, docs, metas, embeddings

# ==========================================
# 2. CLUSTERING (UMAP + HDBSCAN)
# ==========================================
def cluster_task_desc(embeddings: np.ndarray, min_cluster_size: int = 3):
    import umap
    import hdbscan

    n = len(embeddings)
    n_neighbors = max(2, min(15, n - 1))

    reducer_2d = umap.UMAP(n_neighbors=n_neighbors, n_components=2, random_state=42)
    koordinat_2d = reducer_2d.fit_transform(embeddings)

    clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean")
    label_cluster = clusterer.fit_predict(koordinat_2d)

    return koordinat_2d, label_cluster

# ==========================================
# 3. FIGURE SCATTER CLUSTER TASK (Plotly)
# ==========================================
def buat_figure_scatter(koordinat_2d, label_cluster, docs, metas) -> go.Figure:
    warna = [warna_untuk_cluster(c) for c in label_cluster]
    hover_text = [
        f"<b>Cluster {c}</b><br>{doc[:120]}<br><i>status: {m.get('status')}, skor: {m.get('skor')}</i>"
        for c, doc, m in zip(label_cluster, docs, metas)
    ]

    fig = go.Figure(data=go.Scatter(
        x=koordinat_2d[:, 0],
        y=koordinat_2d[:, 1],
        mode="markers",
        marker=dict(size=10, color=warna, line=dict(width=1, color="white")),
        text=hover_text,
        hoverinfo="text",
    ))
    fig.update_layout(
        title="Peta Cluster Task (task_desc) — Voyager Skill Library",
        xaxis_title="UMAP-1", yaxis_title="UMAP-2",
        template="plotly_dark",
    )
    return fig

# ==========================================
# 4. GRAPH TOOL: PARSE + BANGUN
# ==========================================
def parse_trace(trace_raw):
    try:
        data = json.loads(trace_raw) if isinstance(trace_raw, str) else trace_raw
        return [item["name"] if isinstance(item, dict) else str(item) for item in data]
    except Exception:
        if isinstance(trace_raw, str) and "->" in trace_raw:
            return [t.strip() for t in trace_raw.split("->")]
        return []

def bangun_graph_tool(docs, metas, label_cluster):
    """
    Return dict tunggal `graph_data` berisi semua yang dibutuhkan buat
    render + detail panel:
      edge_cluster  : {(a,b): {cluster_id: hitungan}}
      edge_status   : {(a,b): {"berhasil"|"gagal": hitungan}}
      edge_contoh   : {(a,b): [ {task_desc, catatan_hasil, skor, status, cluster}, ... ]}
      node_freq     : {tool: hitungan_total}
      node_status   : {tool: {"berhasil"|"gagal": hitungan}}
      node_contoh   : {tool: [ {...}, ... ]}
    """
    edge_cluster = defaultdict(lambda: defaultdict(int))
    edge_status = defaultdict(lambda: defaultdict(int))
    edge_contoh = defaultdict(list)
    node_freq = defaultdict(int)
    node_status = defaultdict(lambda: defaultdict(int))
    node_contoh = defaultdict(list)

    for doc, meta, cid in zip(docs, metas, label_cluster):
        urutan_tool = parse_trace(meta.get("trace", "[]"))
        status = meta.get("status", "?")
        contoh = {
            "task_desc": doc,
            "catatan_hasil": meta.get("catatan_hasil", ""),
            "skor": meta.get("skor", 0),
            "status": status,
            "cluster": int(cid),
        }
        for t in urutan_tool:
            node_freq[t] += 1
            node_status[t][status] += 1
            node_contoh[t].append(contoh)
        for a, b in zip(urutan_tool, urutan_tool[1:]):
            edge_cluster[(a, b)][int(cid)] += 1
            edge_status[(a, b)][status] += 1
            edge_contoh[(a, b)].append(contoh)

    return {
        "edge_cluster": edge_cluster,
        "edge_status": edge_status,
        "edge_contoh": edge_contoh,
        "node_freq": node_freq,
        "node_status": node_status,
        "node_contoh": node_contoh,
    }

def hitung_jumlah_jembatan(graph_data) -> int:
    return sum(1 for v in graph_data["edge_cluster"].values() if len(v) > 1)

def filter_edges_by_weight(graph_data, min_weight: int):
    """Buang edge yang total kemunculannya di bawah min_weight -- dipakai
    slider filter di tab Streamlit."""
    edge_cluster = {
        k: v for k, v in graph_data["edge_cluster"].items()
        if sum(v.values()) >= min_weight
    }
    keys = set(edge_cluster.keys())
    return {
        "edge_cluster": edge_cluster,
        "edge_status": {k: v for k, v in graph_data["edge_status"].items() if k in keys},
        "edge_contoh": {k: v for k, v in graph_data["edge_contoh"].items() if k in keys},
        "node_freq": graph_data["node_freq"],
        "node_status": graph_data["node_status"],
        "node_contoh": graph_data["node_contoh"],
    }

# ==========================================
# 5. RENDER PYVIS + SUNTIK DETAIL PANEL (klik node/edge)
# ==========================================
def _siapkan_json_detail(node_contoh, edge_contoh, max_contoh: int = 8):
    node_json = {}
    for node, contoh in node_contoh.items():
        urut = sorted(contoh, key=lambda c: -c["skor"])
        node_json[node] = {"total": len(contoh), "contoh": urut[:max_contoh]}

    edge_json = {}
    for (a, b), contoh in edge_contoh.items():
        urut = sorted(contoh, key=lambda c: -c["skor"])
        edge_json[f"{a}||{b}"] = {"total": len(contoh), "contoh": urut[:max_contoh]}

    return node_json, edge_json


_DETAIL_PANEL_TEMPLATE = """
<div id="detail-panel" style="position:fixed; top:12px; right:12px; width:360px;
     max-height:88vh; overflow-y:auto; background:#1b1b1b; color:#eee;
     border:1px solid #444; border-radius:10px; padding:14px;
     font-family:sans-serif; font-size:13px; display:none; z-index:9999;
     box-shadow:0 4px 14px rgba(0,0,0,0.5);">
  <div style="display:flex; justify-content:space-between; align-items:center;">
    <b id="detail-title" style="font-size:14px;">Detail</b>
    <span onclick="document.getElementById('detail-panel').style.display='none'"
          style="cursor:pointer; color:#aaa; padding:2px 6px;">X</span>
  </div>
  <div id="detail-body" style="margin-top:10px;"></div>
</div>
<div style="position:fixed; top:12px; left:12px; color:#888; font-size:12px;
     font-family:sans-serif; background:#1b1b1b; padding:6px 10px; border-radius:6px;">
  Klik node/edge buat lihat contoh task-nya
</div>
<script>
const NODE_DETAILS = __NODE_JSON__;
const EDGE_DETAILS = __EDGE_JSON__;

function tampilkanDetail(judul, data) {
  const panel = document.getElementById('detail-panel');
  const body = document.getElementById('detail-body');
  document.getElementById('detail-title').innerText = judul;
  if (!data) {
    body.innerHTML = '<i style="color:#888;">Tidak ada contoh tercatat (mungkin ke-filter).</i>';
  } else {
    let html = '<div style="color:#999; margin-bottom:8px;">Total kemunculan: ' + data.total + '</div>';
    data.contoh.forEach(function (c) {
      const warnaStatus = c.status === 'berhasil' ? '#2ecc71' : '#e74c3c';
      html += '<div style="border-top:1px solid #333; padding:8px 0;">'
        + '<div style="font-weight:600; line-height:1.35;">' + c.task_desc + '</div>'
        + '<div style="color:' + warnaStatus + '; margin-top:3px;">status: ' + c.status
        + ' | skor: ' + c.skor + ' | cluster: ' + c.cluster + '</div>'
        + '<div style="color:#aaa; font-style:italic; margin-top:3px;">' + (c.catatan_hasil || '') + '</div>'
        + '</div>';
    });
    body.innerHTML = html;
  }
  panel.style.display = 'block';
}

network.on("click", function (params) {
  if (params.nodes.length > 0) {
    const nodeId = params.nodes[0];
    tampilkanDetail('[tool] ' + nodeId, NODE_DETAILS[nodeId]);
  } else if (params.edges.length > 0) {
    const edgeId = String(params.edges[0]);
    tampilkanDetail(edgeId.replace('||', '  ->  '), EDGE_DETAILS[edgeId]);
  }
});
</script>
"""

def buat_html_network(graph_data, color_mode: str = "cluster") -> str:
    """
    color_mode: "cluster" (default, + jembatan emas lintas cluster) atau
    "success_rate" (merah->kuning->hijau berdasarkan rasio berhasil/gagal).
    """
    edge_cluster = graph_data["edge_cluster"]
    edge_status = graph_data["edge_status"]
    node_freq = graph_data["node_freq"]
    node_status = graph_data["node_status"]

    net = Network(height="750px", width="100%", bgcolor="#111111", font_color="white", directed=True)
    net.barnes_hut(gravity=-3000, spring_length=150)

    G = nx.DiGraph()
    for (a, b) in edge_cluster:
        G.add_edge(a, b)

    for node in G.nodes():
        freq = node_freq.get(node, 1)
        if color_mode == "success_rate":
            warna_node = warna_dari_rasio_sukses(rasio_sukses(node_status.get(node, {})))
        else:
            warna_node = "#dddddd"
        net.add_node(node, label=node, value=freq, title=f"{node} (dipakai {freq}x)", color=warna_node)

    for (a, b), per_cluster_count in edge_cluster.items():
        cluster_terlibat = list(per_cluster_count.keys())
        total_weight = sum(per_cluster_count.values())
        is_jembatan = len(cluster_terlibat) > 1

        if color_mode == "success_rate":
            warna_edge = warna_dari_rasio_sukses(rasio_sukses(edge_status.get((a, b), {})))
            dashes = is_jembatan
            tooltip = f"success rate: {round(rasio_sukses(edge_status.get((a, b), {}))*100)}%"
        elif is_jembatan:
            warna_edge, dashes = WARNA_JEMBATAN, True
            tooltip = "JEMBATAN lintas cluster: " + ", ".join(
                f"cluster {c} ({n}x)" for c, n in per_cluster_count.items()
            )
        else:
            warna_edge, dashes = warna_untuk_cluster(cluster_terlibat[0]), False
            tooltip = f"cluster {cluster_terlibat[0]} ({total_weight}x)"

        net.add_edge(a, b, id=f"{a}||{b}", value=total_weight, color=warna_edge,
                     dashes=dashes, title=tooltip, arrows="to")

    net.set_options("""
    {
      "physics": { "stabilization": { "iterations": 200 } },
      "edges": { "smooth": { "type": "dynamic" } },
      "interaction": { "hover": true }
    }
    """)

    html_str = net.generate_html(notebook=False)

    node_json, edge_json = _siapkan_json_detail(graph_data["node_contoh"], graph_data["edge_contoh"])
    panel_html = (
        _DETAIL_PANEL_TEMPLATE
        .replace("__NODE_JSON__", json.dumps(node_json, ensure_ascii=False))
        .replace("__EDGE_JSON__", json.dumps(edge_json, ensure_ascii=False))
    )
    html_str = html_str.replace("</body>", panel_html + "</body>")
    return html_str