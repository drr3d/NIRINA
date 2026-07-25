import json, sqlite3, re
import numpy as np

from typing import List

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_classic.retrievers import EnsembleRetriever
from langchain_core.retrievers import BaseRetriever
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from pydantic import Field

# --- IMPORT BARU UNTUK RERANKER ---
from langchain_classic.retrievers import ContextualCompressionRetriever
from langchain_classic.retrievers.document_compressors import CrossEncoderReranker
from langchain_community.cross_encoders import HuggingFaceCrossEncoder

# --- IMPORT RERANKER ---
from langchain_classic.retrievers import ContextualCompressionRetriever
from langchain_classic.retrievers.document_compressors import CrossEncoderReranker
from langchain_community.cross_encoders import HuggingFaceCrossEncoder

# --- TAMBAHKAN IMPORT INI ---
from .config import db_path, sqlite_db_path

# =====================================================================
# WAJIB IDENTIK DENGAN TEXTPROCESSOR AGAR DIMENSI VEKTOR COCOK (256)
# =====================================================================
class OptimizedCPUEmbeddings(HuggingFaceEmbeddings):
    # set 256 biar lebih ringan, boleh dinaikkan sampai 1024 jika spek mencukupi
    target_dimensions: int = 256

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        prefixed_texts = [f"search_document: {t}" for t in texts]
        embs = super().embed_documents(prefixed_texts)
        return self._truncate_and_normalize(embs)
        
    def embed_query(self, text: str) -> list[float]:
        prefixed_text = f"search_query: {text}"
        emb = super().embed_query(prefixed_text)
        return self._truncate_and_normalize([emb])[0]
        
    def _truncate_and_normalize(self, embeddings: list[list[float]]) -> list[list[float]]:
        arr = np.array(embeddings)[:, :self.target_dimensions]
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        normalized = np.divide(arr, norms, out=np.zeros_like(arr), where=norms!=0)
        return normalized.tolist()

embeddings = None
retriever = None
vector_db = None
embeddings_method = 2 # make sure match with textprocessor.py extraction method

if embeddings_method == 0:
    embeddings = OllamaEmbeddings(model="nomic-embed-text")
    vector_db = Chroma(persist_directory=str(db_path), embedding_function=embeddings)
    # [PERBAIKAN KRUSIAL]: 'k' dinaikkan jadi 10 agar AI bisa membaca seluruh halaman CV
    retriever = vector_db.as_retriever(search_kwargs={"k": 10}) 
elif embeddings_method == 2:
    bge_xmatroyshka = OptimizedCPUEmbeddings(
        model_name="BAAI/bge-m3",
        model_kwargs={'device': 'cpu'},
        encode_kwargs={'normalize_embeddings': False}
    )
    # Load VectorDB dengan Class yang identik
    vector_db = Chroma(
        persist_directory=str(db_path), 
        embedding_function=bge_xmatroyshka,
        collection_metadata={"hnsw:space": "cosine"}
    )

# =====================================================================
# CUSTOM RETRIEVER: SQLITE FTS5 (Pengganti Pickle BM25)
# =====================================================================
class SQLiteFTS5Retriever(BaseRetriever):
    """Retriever kustom LangChain untuk menarik data menggunakan algoritma BM25 dari SQLite FTS5."""
    db_path: str = Field(description="Path ke file database SQLite")
    k: int = Field(default=10, description="Jumlah dokumen yang akan ditarik")

    def _sanitize_fts5_query(self, text: str) -> str:
        """
        Pembersihan teks (Sanitization):
        Mencegah error 'fts5: syntax error' jika AI menggunakan karakter spesial seperti tanda kutip, minus, atau bintang.
        """
        clean_text = re.sub(r'[^\w\s]', ' ', text)
        words = [w.strip() for w in clean_text.split() if w.strip()]
        if not words:
            return ""
        # Format pencarian OR untuk memperluas jangkauan recall BM25
        return " OR ".join([f'"{w}"' for w in words])

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> List[Document]:
        docs = []
        safe_query = self._sanitize_fts5_query(query)
        
        if not safe_query:
            return docs

        try:
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                cursor = conn.cursor()
                # Mencari kecocokan eksak pada kolom 'content' dan diurutkan berdasarkan skor relevansi BM25 (rank)
                cursor.execute(
                    "SELECT content, metadata FROM cv_fts WHERE cv_fts MATCH ? ORDER BY rank LIMIT ?",
                    (safe_query, self.k)
                )
                for row in cursor.fetchall():
                    content = row[0]
                    metadata = json.loads(row[1]) if row[1] else {}
                    docs.append(Document(page_content=content, metadata=metadata))
        except Exception as e:
            print(f"[FTS5 Retriever Error]: {e}")
            
        return docs

# =====================================================================
# HYBRID RETRIEVER (Vector + FTS5 + Reranker)
# =====================================================================
def get_hybrid_retriever(top_k_awal: int = 10, top_k_final: int = 3):
    # 1. Base Retrievers: Makna dari ChromaDB
    chroma_retriever = vector_db.as_retriever(search_kwargs={"k": top_k_awal})
    
    # 2. Base Retrievers: Kata Kunci Eksak dari SQLite FTS5
    fts5_retriever = SQLiteFTS5Retriever(db_path=str(sqlite_db_path), k=top_k_awal)

    # Gabungkan keduanya dengan LangChain Ensemble (Bobot 50:50)
    base_retriever = EnsembleRetriever(
        retrievers=[chroma_retriever, fts5_retriever],
        weights=[0.5, 0.5]
    )

    # 3. Reranker (Menyaring dan mengurutkan ulang hasil Base Retriever agar presisi)
    model_reranker = HuggingFaceCrossEncoder(model_name="BAAI/bge-reranker-base")
    compressor = CrossEncoderReranker(model=model_reranker, top_n=top_k_final)

    # 4. Finalisasi Retriever
    compression_retriever = ContextualCompressionRetriever(
        base_compressor=compressor,
        base_retriever=base_retriever
    )
    
    return compression_retriever