import json
import time
import uuid
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions


class CatatanLibrary:
    """
    Wrapper tipis di atas ChromaDB PersistentClient, koleksi TERPISAH dari
    skill_library maupun tool_library (registry.py) -- skema & tujuan beda,
    sengaja tidak dicampur biar retrieval masing-masing tetap presisi.

    Satu catatan disimpan sebagai:
        document  = isi (INI yang di-embed & dicari kemiripannya -- bukan
                     judul, karena isi lengkap-lah yang mau di-recall nanti)
        metadata  = {"judul": str, "ide_terkait": str,
                     "terkait_dengan": json(list[str]), "tags": json(list[str]),
                     "ts": epoch}
        id        = uuid unik
    """

    def __init__(
        self,
        persist_dir: str = "./catatan_penting_db",
        collection_name: str = "catatan_penting",
        embedding_backend: str = "ollama",  # "ollama" | "st" -- sama filosofi dgn skill_lib.py
        ollama_base_url: str = "http://localhost:11434",
        ollama_model: str = "nomic-embed-text",
        st_model_name: str = "all-MiniLM-L6-v2",
    ):
        self._client = chromadb.PersistentClient(path=persist_dir)

        if embedding_backend == "ollama":
            self._embed_fn = embedding_functions.OllamaEmbeddingFunction(
                url=f"{ollama_base_url}/api/embeddings", model_name=ollama_model,
            )
        elif embedding_backend == "st":
            self._embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=st_model_name
            )
        else:
            raise ValueError(f"Backend embedding tidak dikenal: {embedding_backend!r} (pilih 'ollama'/'st')")

        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            embedding_function=self._embed_fn,
            metadata={"hnsw:space": "cosine"},
        )

    # -------------------------------------
    # WRITE
    # -------------------------------------
    def simpan_catatan(
        self,
        isi: str,
        ide_terkait: str,
        judul: str = "",
        terkait_dengan: Optional[list] = None,
        tags: Optional[list] = None,
    ) -> str:
        """
        isi: teks lengkap jawaban/insight yang mau diingat -- INI yang
            di-embed, tulis selengkap & sejelas mungkin (bukan cuma satu
            baris ringkasan) supaya recall via semantic search nanti akurat.
        ide_terkait: label topik/cluster (mis. "ide explor host A") -- jadi
            "node besar" saat divisualisasikan sebagai graf nanti. Pakai
            label yang SAMA PERSIS kalau memang masih ide yang sama (lihat
            daftar_ide() sebelum bikin label baru, biar tidak ada 2 label
            untuk 1 ide yang sama, mis. "ide explor host A" vs "eksplorasi
            host A").
        judul: judul pendek manusiawi, opsional -- kalau kosong, diambil
            otomatis dari 60 karakter pertama `isi`.
        terkait_dengan: list label `ide_terkait` LAIN yang berhubungan
            dengan catatan ini (mis. catatan "ide explor host A" bisa
            terkait_dengan=["ide exploit host A"]) -- ini yang jadi EDGE
            antar node saat divisualisasikan sebagai graf nanti. Opsional.
        tags: label bebas tambahan (mis. ["nmap","cve"]), opsional -- buat
            filter/pencarian tambahan di luar `ide_terkait`.
        """
        catatan_id = str(uuid.uuid4())
        judul_final = judul.strip() if judul and judul.strip() else (isi or "")[:60]
        self._collection.add(
            ids=[catatan_id],
            documents=[isi],
            metadatas=[{
                "judul": judul_final,
                "ide_terkait": ide_terkait,
                "terkait_dengan": json.dumps(terkait_dengan or [], ensure_ascii=False),
                "tags": json.dumps(tags or [], ensure_ascii=False),
                "ts": time.time(),
            }],
        )
        return catatan_id

    # -------------------------------------
    # READ
    # -------------------------------------
    def cari_catatan(
        self,
        query: str,
        ide_terkait: Optional[str] = None,
        top_k: int = 5,
        min_similarity: float = 0.0,
    ) -> list[dict]:
        """Semantic search di isi catatan. Kalau `ide_terkait` diisi, hasil
        dibatasi HANYA catatan dengan label ide itu persis (exact match) --
        berguna kalau AI/user sudah tahu mau recall dari cluster ide mana."""
        where = {"ide_terkait": ide_terkait} if ide_terkait else None
        n_koleksi = self._collection.count()
        if n_koleksi == 0:
            return []

        hasil = self._collection.query(
            query_texts=[query],
            n_results=min(top_k * 2, n_koleksi),
            where=where,
        )

        catatan = []
        docs = hasil.get("documents", [[]])[0]
        metas = hasil.get("metadatas", [[]])[0]
        dists = hasil.get("distances", [[]])[0]
        ids = hasil.get("ids", [[]])[0]

        for cid, doc, meta, dist in zip(ids, docs, metas, dists):
            similarity = 1 - dist
            if similarity < min_similarity:
                continue
            catatan.append({
                "id": cid,
                "judul": meta.get("judul", ""),
                "isi": doc,
                "ide_terkait": meta.get("ide_terkait", ""),
                "terkait_dengan": json.loads(meta.get("terkait_dengan", "[]")),
                "tags": json.loads(meta.get("tags", "[]")),
                "similarity": round(similarity, 4),
            })

        catatan.sort(key=lambda c: c["similarity"], reverse=True)
        return catatan[:top_k]

    def daftar_ide(self) -> list[str]:
        """Ambil SEMUA label `ide_terkait` unik yang sudah pernah dipakai --
        panggil ini SEBELUM simpan_catatan() untuk cek apakah ide yang mau
        dipakai sudah ada label-nya (biar tidak duplikat, mis. "ide explor
        host A" vs "eksplorasi host A" dianggap 2 ide beda padahal sama)."""
        n_koleksi = self._collection.count()
        if n_koleksi == 0:
            return []
        semua = self._collection.get(include=["metadatas"])
        ide_set = {m.get("ide_terkait", "") for m in semua.get("metadatas", []) if m.get("ide_terkait")}
        return sorted(ide_set)

    def ambil_semua_untuk_graf(self) -> list[dict]:
        """Ambil SELURUH catatan (tanpa similarity search) dalam bentuk siap
        pakai buat visualisasi graf (networkx atau sejenisnya): tiap catatan
        = satu node, `ide_terkait` dipakai buat clustering/warna node, dan
        `terkait_dengan` dipakai buat menggambar edge antar node/cluster.
        Contoh pemakaian dgn networkx (di luar file ini, di kode viewer-mu):

            import networkx as nx
            G = nx.Graph()
            for c in catatan_lib.ambil_semua_untuk_graf():
                G.add_node(c["id"], label=c["judul"], cluster=c["ide_terkait"])
                for ide_lain in c["terkait_dengan"]:
                    G.add_edge(c["ide_terkait"], ide_lain)
        """
        n_koleksi = self._collection.count()
        if n_koleksi == 0:
            return []
        semua = self._collection.get(include=["documents", "metadatas"])
        hasil = []
        for cid, doc, meta in zip(semua.get("ids", []), semua.get("documents", []), semua.get("metadatas", [])):
            hasil.append({
                "id": cid,
                "judul": meta.get("judul", ""),
                "isi": doc,
                "ide_terkait": meta.get("ide_terkait", ""),
                "terkait_dengan": json.loads(meta.get("terkait_dengan", "[]")),
                "tags": json.loads(meta.get("tags", "[]")),
                "ts": meta.get("ts", 0),
            })
        return hasil

    def hapus_catatan(self, catatan_id: str) -> bool:
        """Hapus SATU catatan by id -- dipakai kalau ternyata catatan yang
        tersimpan keliru/basi. Return True kalau ID ditemukan & dihapus."""
        existing = self._collection.get(ids=[catatan_id])
        if not existing.get("ids"):
            return False
        self._collection.delete(ids=[catatan_id])
        return True