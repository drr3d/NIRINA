from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Optional


# ==========================================
# --- KONTRAK ADAPTER ---
# ==========================================
class VectorStoreAdapter(ABC):
    @abstractmethod
    def add(self, id_: str, embedding: list, document: str, metadata: dict) -> None:
        """Simpan SATU entry baru (upsert -- kalau `id_` sudah ada, timpa)."""
        ...

    @abstractmethod
    def query(self, embedding: list, n_results: int, where: Optional[dict] = None) -> list:
        """
        Cari `n_results` entry PALING MIRIP `embedding`, opsional difilter
        exact-match `where` (SATU key, mis. {"status": "berhasil"} --
        skill_lib.py tidak pernah pakai lebih dari 1 key).

        Return: list of {"id": str, "document": str, "metadata": dict,
        "distance": float}, urut dari PALING MIRIP (distance terkecil),
        panjang list <= n_results. `distance` HARUS cosine distance.
        """
        ...

    @abstractmethod
    def get(self, where: Optional[dict] = None) -> list:
        """Ambil SEMUA entry (opsional difilter exact-match `where`).
        Return: list of {"id": str, "metadata": dict} -- TIDAK perlu
        `document`/`embedding` (hapus_skill_terkait_tool cuma butuh
        id+metadata utk decode `trace`)."""
        ...

    @abstractmethod
    def delete(self, ids: list) -> None:
        """Hapus banyak entry sekaligus by id. No-op kalau `ids` kosong/None."""
        ...

    @abstractmethod
    def count(self, where: Optional[dict] = None) -> int:
        """Jumlah entry di koleksi/collection/table ini, opsional difilter
        exact-match `where` (SATU key, sama seperti `query()`/`get()`).
        `where=None` (default) = jumlah TOTAL, perilaku identik dgn versi
        sebelum parameter ini ada -- 100% backward-compatible utk caller
        lama yg manggil `count()` tanpa argumen."""
        ...


def _filter_tunggal(where: Optional[dict]):
    """Helper kecil dipakai semua adapter -- skill_lib.py SELALU cuma
    pernah kirim `where` dgn NOL atau SATU key (mis. {"status": "..."}),
    jadi aman diasumsikan di sini. Return (key, value) atau (None, None)."""
    if not where:
        return None, None
    (k, v), = where.items()
    return k, v


# ==========================================
# --- BACKEND: CHROMADB (DEFAULT) ---
# ==========================================
class ChromaAdapter(VectorStoreAdapter):
    def __init__(self, persist_dir: str, collection_name: str):
        import chromadb  # lazy import -- opsional kalau backend lain yang dipakai

        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, id_, embedding, document, metadata):
        self._collection.add(
            ids=[id_], embeddings=[embedding], documents=[document], metadatas=[metadata],
        )

    def query(self, embedding, n_results, where=None):
        n_koleksi = self._collection.count()
        if n_koleksi == 0:
            return []
        hasil = self._collection.query(
            query_embeddings=[embedding],
            n_results=min(n_results, n_koleksi),
            where=where,
        )
        ids = hasil.get("ids", [[]])[0]
        docs = hasil.get("documents", [[]])[0]
        metas = hasil.get("metadatas", [[]])[0]
        dists = hasil.get("distances", [[]])[0]
        return [
            {"id": id_, "document": doc, "metadata": meta, "distance": dist}
            for id_, doc, meta, dist in zip(ids, docs, metas, dists)
        ]

    def get(self, where=None):
        hasil = self._collection.get(where=where, include=["metadatas"])
        return [
            {"id": id_, "metadata": meta}
            for id_, meta in zip(hasil.get("ids", []), hasil.get("metadatas", []))
        ]

    def delete(self, ids):
        if ids:
            self._collection.delete(ids=list(ids))

    def count(self, where=None) -> int:
        if not where:
            return self._collection.count()
        # ids-only, TANPA metadatas/documents/embeddings -- lebih murah dari get() biasa
        return len(self._collection.get(where=where, include=[]).get("ids", []))


# ==========================================
# --- BACKEND: MILVUS ---
# ==========================================
class MilvusAdapter(VectorStoreAdapter):
    def __init__(
        self,
        embedding_dim: int,
        collection_name: str = "agent_skills",
        uri: str = "http://localhost:19530",
        token: str = "",
    ):
        from pymilvus import DataType, MilvusClient

        self._client = MilvusClient(uri=uri, token=token)
        self._collection_name = collection_name

        if not self._client.has_collection(collection_name):
            schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field(field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field(field_name="embedding", datatype=DataType.FLOAT_VECTOR, dim=embedding_dim)
            schema.add_field(field_name="document", datatype=DataType.VARCHAR, max_length=65535)
            schema.add_field(field_name="metadata", datatype=DataType.JSON)

            index_params = self._client.prepare_index_params()
            index_params.add_index(field_name="embedding", index_type="AUTOINDEX", metric_type="COSINE")

            self._client.create_collection(
                collection_name=collection_name, schema=schema, index_params=index_params,
            )

    @staticmethod
    def _bangun_filter_expr(where: Optional[dict]) -> str:
        k, v = _filter_tunggal(where)
        if k is None:
            return ""
        # Expr Milvus utk field JSON: metadata["key"] == "value" -- json.dumps
        # dipakai murni utk quoting string yang aman (bukan buat parsing JSON).
        return f'metadata["{k}"] == {json.dumps(v)}'

    def add(self, id_, embedding, document, metadata):
        self._client.insert(
            collection_name=self._collection_name,
            data=[{"id": id_, "embedding": embedding, "document": document, "metadata": metadata}],
        )

    def query(self, embedding, n_results, where=None):
        hasil = self._client.search(
            collection_name=self._collection_name,
            data=[embedding],
            limit=n_results,
            filter=self._bangun_filter_expr(where),
            output_fields=["document", "metadata"],
        )
        out = []
        for hit in hasil[0] if hasil else []:
            entity = hit.get("entity", hit)
            # Milvus metric_type=COSINE me-return cosine SIMILARITY (bukan
            # distance) di field 'distance' hasil search -- balik jadi
            # distance (1 - similarity) supaya konsisten dgn caller di
            # skill_lib.py yang selalu hitung `similarity = 1 - distance`.
            out.append({
                "id": hit["id"],
                "document": entity.get("document", ""),
                "metadata": entity.get("metadata", {}),
                "distance": 1 - hit["distance"],
            })
        return out

    def get(self, where=None):
        hasil = self._client.query(
            collection_name=self._collection_name,
            filter=self._bangun_filter_expr(where) or 'id != ""',
            output_fields=["metadata"],
        )
        return [{"id": row["id"], "metadata": row.get("metadata", {})} for row in hasil]

    def delete(self, ids):
        if ids:
            self._client.delete(collection_name=self._collection_name, ids=list(ids))

    def count(self, where=None) -> int:
        if not where:
            stats = self._client.get_collection_stats(self._collection_name)
            return int(stats.get("row_count", 0))
        hasil = self._client.query(
            collection_name=self._collection_name,
            filter=self._bangun_filter_expr(where),
            output_fields=["id"],
        )
        return len(hasil)


# ==========================================
# --- BACKEND: POSTGRES + PGVECTOR ---
# ==========================================
class PgVectorAdapter(VectorStoreAdapter):
    def __init__(self, embedding_dim: int, dsn: str, table_name: str = "agent_skills"):
        import psycopg2
        import psycopg2.extras
        from pgvector.psycopg2 import register_vector

        self._psycopg2 = psycopg2
        self._table = table_name
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = True

        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        register_vector(self._conn)

        with self._conn.cursor() as cur:
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {self._table} (
                        id TEXT PRIMARY KEY,
                        document TEXT NOT NULL,
                        embedding VECTOR({embedding_dim}) NOT NULL,
                        metadata JSONB NOT NULL
                    );"""
            )
            cur.execute(
                f"""CREATE INDEX IF NOT EXISTS {self._table}_embedding_hnsw_cosine
                    ON {self._table} USING hnsw (embedding vector_cosine_ops);"""
            )

    def add(self, id_, embedding, document, metadata):
        with self._conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {self._table} (id, document, embedding, metadata)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        document = EXCLUDED.document,
                        embedding = EXCLUDED.embedding,
                        metadata = EXCLUDED.metadata;""",
                (id_, document, embedding, self._psycopg2.extras.Json(metadata)),
            )

    def query(self, embedding, n_results, where=None):
        k, v = _filter_tunggal(where)
        klausa_where = "WHERE metadata->>%s = %s" if k is not None else ""
        params = [embedding] + ([k, str(v)] if k is not None else []) + [n_results]

        sql = (
            f"SELECT id, document, metadata, embedding <=> %s AS distance "
            f"FROM {self._table} {klausa_where} ORDER BY distance ASC LIMIT %s;"
        )
        with self._conn.cursor(cursor_factory=self._psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            {"id": r["id"], "document": r["document"], "metadata": r["metadata"], "distance": float(r["distance"])}
            for r in rows
        ]

    def get(self, where=None):
        k, v = _filter_tunggal(where)
        klausa_where = "WHERE metadata->>%s = %s" if k is not None else ""
        params = [k, str(v)] if k is not None else []
        sql = f"SELECT id, metadata FROM {self._table} {klausa_where};"
        with self._conn.cursor(cursor_factory=self._psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [{"id": r["id"], "metadata": r["metadata"]} for r in rows]

    def delete(self, ids):
        if not ids:
            return
        with self._conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self._table} WHERE id = ANY(%s);", (list(ids),))

    def count(self, where=None) -> int:
        k, v = _filter_tunggal(where)
        klausa_where = "WHERE metadata->>%s = %s" if k is not None else ""
        params = [k, str(v)] if k is not None else []
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {self._table} {klausa_where};", params)
            return cur.fetchone()[0]


# ==========================================
# --- FACTORY ---
# ==========================================
def buat_vector_adapter(
    backend: str,
    embedding_dim: int,
    persist_dir: str = "./skill_library_db",
    collection_name: str = "agent_skills",
    milvus_uri: str = "http://localhost:19530",
    milvus_token: str = "",
    pg_dsn: Optional[str] = None,
    custom_adapter: Optional[VectorStoreAdapter] = None,
) -> VectorStoreAdapter:
    """
    Factory adapter -- SATU pintu masuk yang dipanggil `skill_lib.py`.
    `backend`:
      - "chroma"   (DEFAULT, backward-compatible): pakai `persist_dir` +
                   `collection_name`, TIDAK butuh dependency tambahan
                   apapun di luar yang sudah ada.
      - "milvus"   : pakai `milvus_uri` (+ `milvus_token` kalau perlu auth,
                   mis. Zilliz Cloud) + `collection_name`.
      - "pgvector" : `pg_dsn` WAJIB diisi (connection string Postgres,
                   mis. "postgresql://user:pass@host:5432/db") +
                   `collection_name` dipakai sebagai nama tabel.
      - "custom"   : `custom_adapter` yang Anda buat sendiri (harus
                   instance subclass VectorStoreAdapter) -- jalur ekstensi
                   ke backend lain di luar 3 di atas tanpa perlu ubah file
                   ini lagi.
    """
    if backend == "chroma":
        return ChromaAdapter(persist_dir=persist_dir, collection_name=collection_name)
    elif backend == "milvus":
        return MilvusAdapter(
            embedding_dim=embedding_dim, collection_name=collection_name,
            uri=milvus_uri, token=milvus_token,
        )
    elif backend == "pgvector":
        if not pg_dsn:
            raise ValueError("backend='pgvector' butuh pg_dsn (connection string Postgres).")
        return PgVectorAdapter(embedding_dim=embedding_dim, dsn=pg_dsn, table_name=collection_name)
    elif backend == "custom":
        if custom_adapter is None:
            raise ValueError("backend='custom' butuh custom_adapter (instance VectorStoreAdapter).")
        return custom_adapter
    else:
        raise ValueError(
            f"Backend vector store tidak dikenal: {backend!r} (pilih 'chroma'/'milvus'/'pgvector'/'custom')"
        )