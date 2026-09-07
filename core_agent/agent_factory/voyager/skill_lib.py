import hashlib
import json
import time
import uuid
from typing import Callable, Optional

from chromadb.utils import embedding_functions

from .vector_adapters import buat_vector_adapter
def format_trace_polos(trace: list) -> str:
    return " -> ".join(
        t.get("name", "?") if isinstance(t, dict) else str(t) for t in (trace or [])
    )

def format_trace_proba_heuristik(trace: list, skala_100: bool = True, presisi: int = 0) -> str:
    netral = "100" if skala_100 else f"{1.0:.{presisi}f}"
    bagian = []
    for t in (trace or []):
        if isinstance(t, dict):
            conf = t.get("confidence")
            skor = conf.get("skor_confidence") if isinstance(conf, dict) else None
            if skor is None:
                bagian.append("?")
            else:
                nilai = skor * 100 if skala_100 else skor
                bagian.append(f"{nilai:.{presisi}f}")
        else:
            bagian.append(netral)
    return " -> ".join(bagian)


def format_trace_proba_llm(trace: list, skala_100: bool = True, presisi: int = 0) -> str:
    netral = "100" if skala_100 else f"{1.0:.{presisi}f}"
    bagian = []
    for t in (trace or []):
        if isinstance(t, dict):
            conf = t.get("confidence")
            skor = conf.get("skor_confidence_llm") if isinstance(conf, dict) else None
            if skor is None:
                bagian.append("?")
            else:
                nilai = skor * 100 if skala_100 else skor
                bagian.append(f"{nilai:.{presisi}f}")
        else:
            bagian.append(netral)
    return " -> ".join(bagian)


def format_trace_dgn_confidence(trace: list, presisi: int = 2) -> str:
    bagian = []
    for t in (trace or []):
        if isinstance(t, dict):
            nama = t.get("name", "?")
            conf = t.get("confidence")
            skor = conf.get("skor_confidence") if isinstance(conf, dict) else None
            if skor is not None:
                bagian.append(f"{nama}{{confidence:{skor:.{presisi}f}}}")
            else:
                bagian.append(nama)
        else:
            bagian.append(str(t))
    return " -> ".join(bagian)


def ekstrak_confidence_per_langkah(trace: list) -> list:
    hasil = []
    step = 0
    for t in (trace or []):
        if not isinstance(t, dict):
            continue
        baris = {"step": step, "name": t.get("name")}
        conf = t.get("confidence")
        if isinstance(conf, dict):
            baris.update(conf)
        hasil.append(baris)
        step += 1
    return hasil


# ==========================================
# --- EMBEDDING BACKEND FACTORY ---
# ==========================================
def _buat_embedding_fn(
    backend: str,
    ollama_base_url: str = "http://localhost:11434",
    ollama_model: str = "nomic-embed-text",
    st_model_name: str = "all-MiniLM-L6-v2",
    custom_fn: Optional[Callable] = None,
):
    if backend == "ollama":
        return embedding_functions.OllamaEmbeddingFunction(
            url=f"{ollama_base_url}/api/embeddings",
            model_name=ollama_model,
        )
    elif backend == "st":
        return embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=st_model_name
        )
    elif backend == "custom":
        if custom_fn is None:
            raise ValueError("backend='custom' butuh custom_fn (embedding function callable).")
        return custom_fn
    else:
        raise ValueError(f"Backend embedding tidak dikenal: {backend!r} (pilih 'ollama'/'st'/'custom')")


# ==========================================
# --- SKILL LIBRARY ---
# ==========================================
class SkillLibrary:
    def __init__(
        self,
        persist_dir: str = "./skill_library_db",
        collection_name: str = "agent_skills",
        embedding_backend: str = "ollama",  # "ollama" | "st" | "custom"
        ollama_base_url: str = "http://localhost:11434",
        ollama_model: str = "nomic-embed-text",
        st_model_name: str = "all-MiniLM-L6-v2",
        custom_embedding_fn: Optional[Callable] = None,
        # --- [BARU] Pilihan backend vector-store, lihat vector_adapters.py ---
        vector_backend: str = "chroma",  # "chroma" (default) | "milvus" | "pgvector" | "custom"
        milvus_uri: str = "http://localhost:19530",
        milvus_token: str = "",
        pg_dsn: Optional[str] = None,
        custom_vector_adapter: Optional[object] = None,
    ):
        self._embed_fn = _buat_embedding_fn(
            backend=embedding_backend,
            ollama_base_url=ollama_base_url,
            ollama_model=ollama_model,
            st_model_name=st_model_name,
            custom_fn=custom_embedding_fn,
        )

        self._embedding_dim = len(self._embed_fn(["__probe_dimensi__"])[0])

        self._store = buat_vector_adapter(
            backend=vector_backend,
            embedding_dim=self._embedding_dim,
            persist_dir=persist_dir,
            collection_name=collection_name,
            milvus_uri=milvus_uri,
            milvus_token=milvus_token,
            pg_dsn=pg_dsn,
            custom_adapter=custom_vector_adapter,
        )

    # -------------------------------------
    # WRITE
    # -------------------------------------
    def simpan_skill(
        self,
        deskripsi_task: str,
        trace: list,
        catatan_hasil: str = "",
        status: str = "berhasil",  # "berhasil" | "gagal"
        skor: int = 0,
        rag_candidates_trace: Optional[list] = None,
        ringkasan_confidence: Optional[dict] = None,
    ) -> str:

        skill_id = str(uuid.uuid4())
        embedding = self._embed_fn([deskripsi_task])[0]
        self._store.add(
            id_=skill_id,
            embedding=embedding,
            document=deskripsi_task,
            metadata={
                "deskripsi_task": deskripsi_task,
                "trace": json.dumps(trace, default=str, ensure_ascii=False),
                "catatan_hasil": catatan_hasil,
                "status": status,
                "skor": skor,
                "rag_candidates_trace": json.dumps(rag_candidates_trace or [], default=str, ensure_ascii=False),
                "ringkasan_confidence": json.dumps(ringkasan_confidence or {}, default=str, ensure_ascii=False),
                "ts": time.time(),
            },
        )
        return skill_id

    # -------------------------------------
    # READ
    # -------------------------------------
    def cari_skill_relevan(
        self,
        deskripsi_task_baru: str,
        top_k: int = 3,
        status_filter: str = "berhasil", # <-- FIX: Ubah dari boolean agar bisa filter "gagal"
        min_similarity: float = 0.0,
        maks_umur_detik: Optional[float] = None,  # [BARU] lihat catatan di bawah
    ) -> list[dict]:
        where = {"status": status_filter} if status_filter else None

        n_koleksi = self._store.count()
        if n_koleksi == 0:
            return []

        jumlah_kandidat = min(top_k * 3, n_koleksi)
        if maks_umur_detik is not None:
            jumlah_kandidat = min(jumlah_kandidat * 3, n_koleksi)

        embedding = self._embed_fn([deskripsi_task_baru])[0]
        hasil = self._store.query(embedding, n_results=jumlah_kandidat, where=where)

        skills = []
        waktu_sekarang = time.time()
        for entry in hasil:
            meta = entry["metadata"]
            similarity = 1 - entry["distance"]
            if similarity < min_similarity:
                continue

            # [BARU] Buang kalau sudah kedaluwarsa
            if maks_umur_detik is not None:
                umur = waktu_sekarang - meta.get("ts", 0)
                if umur > maks_umur_detik:
                    continue

            skor = meta.get("skor", 0)

            skills.append({
                "deskripsi": entry["document"],
                "trace": json.loads(meta.get("trace", "[]")),
                "catatan_hasil": meta.get("catatan_hasil", ""),
                "status": meta.get("status", ""),
                "skor": skor,
                "similarity": round(similarity, 4),
                "ringkasan_confidence": json.loads(meta.get("ringkasan_confidence", "{}")),
            })

        BOBOT_SKOR = 0.15
        for s in skills:
            bonus_skor = (s["skor"] / 100.0) * BOBOT_SKOR
            s["final_rank_score"] = s["similarity"] + bonus_skor

        n_konfirmasi = len(skills)
        langkah_minimum_diketahui = min((len(s["trace"]) for s in skills), default=None)
        for s in skills:
            s["n_konfirmasi"] = n_konfirmasi
            s["langkah_minimum_diketahui"] = langkah_minimum_diketahui

        skills.sort(key=lambda x: x["final_rank_score"], reverse=True)

        return skills[:top_k]

    # -------------------------------------
    # MINING SUPPORT [BARU]
    # -------------------------------------
    def hitung_skill(self, status_filter: Optional[str] = None) -> int:
        where = {"status": status_filter} if status_filter else None
        return self._store.count(where=where)

    def semua_skill_untuk_mining(self, status_filter: str = "berhasil") -> list[dict]:
        where = {"status": status_filter} if status_filter else None
        semua = self._store.get(where=where)

        hasil = []
        for entry in semua:
            meta = entry["metadata"]
            try:
                trace = json.loads(meta.get("trace", "[]"))
            except (json.JSONDecodeError, TypeError):
                continue
            hasil.append({
                "deskripsi": meta.get("deskripsi_task", ""),
                "trace": trace,
            })
        return hasil

    # -------------------------------------
    # PURGE MANUAL [BARU]
    # -------------------------------------
    def hapus_skill_terkait_tool(self, nama_tool: str, hanya_status: Optional[str] = None) -> int:
        where = {"status": hanya_status} if hanya_status else None
        semua = self._store.get(where=where)

        ids_hapus = []
        for entry in semua:
            meta = entry["metadata"]
            try:
                trace = json.loads(meta.get("trace", "[]"))
            except (json.JSONDecodeError, TypeError):
                continue
            nama_di_trace = {
                (t.get("name") if isinstance(t, dict) else str(t)) for t in trace
            }
            if nama_tool in nama_di_trace:
                ids_hapus.append(entry["id"])

        if ids_hapus:
            self._store.delete(ids_hapus)

        return len(ids_hapus)

    def format_untuk_prompt(self, skills_sukses: list[dict], skills_gagal: list[dict] = None) -> str:
        if not skills_sukses and not skills_gagal:
            return ""

        blok = []

        # --- POINT 3: Instruksi Sistematis Pencegah Hardcoding (Abstraksi Parameter) ---
        blok.append(
            "--- SKILL LIBRARY: REFERENSI MASA LALU (LATAR BELAKANG, BUKAN INSTRUKSI) ---\n"
            "⚠️ Ini catatan dari task-task SEBELUMNYA, sekadar LATAR BELAKANG/OPSIONAL --"
            " BUKAN instruksi untuk task SEKARANG. Instruksi eksplisit dari user pada "
            "pesan-pesan SEBELUM blok ini SELALU yang menentukan apa yang harus kamu "
            "lakukan. Kalau instruksi user (urutan langkah, tool yang diminta, dll) "
            "BERBEDA dari referensi di bawah, ABAIKAN referensi ini sepenuhnya dan "
            "ikuti instruksi user apa adanya. Argumen (IP, nama host, file, dll) di "
            "referensi ini juga DATA DARI TUGAS MASA LALU -- WAJIB disesuaikan dengan "
            "instruksi tugas SAAT INI, jangan pernah disalin buta."
        )

        if skills_sukses:
            blok.append("\n✅ CONTOH PENDEKATAN YANG DULU BERHASIL (ilustrasi saja, BUKAN keharusan diulang):")
            for i, s in enumerate(skills_sukses, 1):
                urutan_tool = format_trace_polos(s["trace"])
                skor_teks = f"{s.get('skor', 0)}/100"
                blok.append(
                    f"  [Skill {i} | Sim: {s['similarity']} | Skor: {skor_teks}] Task: \"{s['deskripsi']}\"\n"
                    f"  Alur eksekusi: {urutan_tool}\n"
                    f"  Catatan: {s['catatan_hasil']}"
                )

        # --- POINT 2: Format Memori Negatif (Negative Constraints) ---
        if skills_gagal:
            blok.append("\n❌ PENDEKATAN YANG DULU GAGAL (hindari MENGULANG kesalahan yang sama, tapi ini juga bukan alasan menolak instruksi user saat ini):")
            for i, s in enumerate(skills_gagal, 1):
                urutan_tool = format_trace_polos(s["trace"])
                blok.append(
                    f"  [Gagal {i} | Sim: {s['similarity']}] Task: \"{s['deskripsi']}\"\n"
                    f"  Alur yang salah: {urutan_tool}\n"
                    f"  Alasan gagal: {s['catatan_hasil']}"
                )

        blok.append(
            "-----------------------------------------------------------------\n"
            "🔴 SEKALI LAGI: seluruh isi blok di atas cuma LATAR BELAKANG. Instruksi "
            "user yang eksplisit di pesan sebelumnya SELALU prioritas utama -- "
            "kalau bertentangan, ikuti instruksi user, bukan referensi di atas."
        )
        return "\n".join(blok)


# ==========================================
# --- PATTERN LIBRARY [BARU] ---
# ==========================================
class PatternLibrary:
    def __init__(
        self,
        persist_dir: str = "./pattern_library_db",
        collection_name: str = "agent_patterns",
        embedding_backend: str = "ollama",
        ollama_base_url: str = "http://localhost:11434",
        ollama_model: str = "nomic-embed-text",
        st_model_name: str = "all-MiniLM-L6-v2",
        custom_embedding_fn: Optional[Callable] = None,
        vector_backend: str = "chroma",
        milvus_uri: str = "http://localhost:19530",
        milvus_token: str = "",
        pg_dsn: Optional[str] = None,
        custom_vector_adapter: Optional[object] = None,
    ):
        self._embed_fn = _buat_embedding_fn(
            backend=embedding_backend,
            ollama_base_url=ollama_base_url,
            ollama_model=ollama_model,
            st_model_name=st_model_name,
            custom_fn=custom_embedding_fn,
        )
        self._embedding_dim = len(self._embed_fn(["__probe_dimensi__"])[0])
        self._store = buat_vector_adapter(
            backend=vector_backend,
            embedding_dim=self._embedding_dim,
            persist_dir=persist_dir,
            collection_name=collection_name,
            milvus_uri=milvus_uri,
            milvus_token=milvus_token,
            pg_dsn=pg_dsn,
            custom_adapter=custom_vector_adapter,
        )

    @staticmethod
    def _id_dari_gram(gram) -> str:
        raw = json.dumps(list(gram), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    # -------------------------------------
    # WRITE (upsert manual by deterministic id)
    # -------------------------------------
    def simpan_pola(
        self,
        gram: tuple,
        deskripsi_abstrak: str,
        task_asal,
        support: int,
        contoh_entry: Optional[dict] = None,
    ) -> str:
        pola_id = self._id_dari_gram(gram)
        embedding = self._embed_fn([deskripsi_abstrak])[0]

        self._store.delete([pola_id])
        self._store.add(
            id_=pola_id,
            embedding=embedding,
            document=deskripsi_abstrak,
            metadata={
                "gram": json.dumps(list(gram), ensure_ascii=False),
                "task_asal": json.dumps(list(task_asal), default=str, ensure_ascii=False),
                "support": support,
                "contoh_entry": json.dumps(contoh_entry, default=str, ensure_ascii=False) if contoh_entry else "",
                "ts": time.time(),
            },
        )
        return pola_id

    _ID_WATERMARK = "__mining_watermark__"

    def set_watermark_mining(self, total_berhasil_saat_ini: int) -> None:
        embedding = self._embed_fn(["__penanda_watermark_internal__"])[0]
        try:
            self._store.delete([self._ID_WATERMARK])
        except Exception:
            pass
        self._store.add(
            id_=self._ID_WATERMARK,
            embedding=embedding,
            document="__watermark__",
            metadata={"tipe": "watermark", "last_mined_count": total_berhasil_saat_ini},
        )

    def ambil_watermark_mining(self) -> int:
        hasil = self._store.get(where={"tipe": "watermark"})
        for entry in hasil:
            if entry.get("id") == self._ID_WATERMARK:
                return int(entry["metadata"].get("last_mined_count", 0))
        return 0

    # -------------------------------------
    # READ
    # -------------------------------------
    def cari_pola_relevan(
        self,
        deskripsi_task_baru: str,
        top_k: int = 2,
        min_similarity: float = 0.72,
    ) -> list[dict]:
        n_koleksi = self._store.count()
        if n_koleksi == 0:
            return []

        embedding = self._embed_fn([deskripsi_task_baru])[0]
        hasil = self._store.query(embedding, n_results=min(top_k * 3, n_koleksi), where=None)

        pola = []
        for entry in hasil:
            meta = entry["metadata"]
            if meta.get("tipe") == "watermark":
                continue  # [PENTING] entry bookkeeping internal, BUKAN pola beneran -- jangan pernah lolos ke hasil
            similarity = 1 - entry["distance"]
            if similarity < min_similarity:
                continue
            pola.append({
                "deskripsi_abstrak": entry["document"],
                "gram": json.loads(meta.get("gram", "[]")),
                "task_asal": json.loads(meta.get("task_asal", "[]")),
                "support": meta.get("support", 0),
                "similarity": round(similarity, 4),
            })

        pola.sort(key=lambda x: x["similarity"], reverse=True)
        return pola[:top_k]