import pickle
import numpy as np
from pathlib import Path

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.retrievers import BM25Retriever
from langchain_classic.retrievers import EnsembleRetriever

# --- TAMBAHKAN IMPORT INI ---
from .config import db_path

# =====================================================================
# WAJIB IDENTIK DENGAN TEXTPROCESSOR AGAR DIMENSI VEKTOR COCOK (256)
# =====================================================================
class OptimizedCPUEmbeddings(HuggingFaceEmbeddings):
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
# [HYBRID RETRIEVER EXPORT] Gantikan `retriever` lama Anda dengan ini
# =====================================================================
def get_hybrid_retriever(top_k: int = 10):
    chroma_retriever = vector_db.as_retriever(search_kwargs={"k": top_k})
    
    bm25_corpus_path = Path(str(db_path)) / "bm25_corpus.pkl"
    bm25_retriever = None
    
    if bm25_corpus_path.exists():
        try:
            with open(bm25_corpus_path, "rb") as f:
                corpus_docs = pickle.load(f)
            if corpus_docs:
                bm25_retriever = BM25Retriever.from_documents(corpus_docs)
                bm25_retriever.k = top_k
        except Exception as e:
            print(f"[Warning] Gagal meload BM25: {e}")

    # Gabungkan Makna (Vector) dan Kata Kunci (BM25)
    if bm25_retriever:
        return EnsembleRetriever(
            retrievers=[chroma_retriever, bm25_retriever],
            weights=[0.5, 0.5]
        )
    return chroma_retriever