import os, sys, json, logging
import importlib
import streamlit as st
import sqlite3

from typing import Dict, Any, List, Type
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.graph import StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver

from .config import sqlite_db_path
from .agent_nodes import (
    AgentState
)

# ==========================================
# LOGGING
# ==========================================
logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)  # eksplisit stdout, samain persis dgn print() lama
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # jangan diteruskan ke root logger (hindari baris ke-print dua kali)

# ==========================================
# HITL QUEUE -- MURNI PERSISTENCE, TIDAK TAHU APA-APA SOAL LANGGRAPH
# ==========================================
class HitlQueue:
    """
    Satu-satunya tanggung jawab: tabel `hitl_queue`. Inisialisasi/migrasi
    skema, mencatat state yang tertahan (butuh persetujuan), dan
    memperbarui status saat user Setuju/Batal. Sengaja dipisah dari
    AgenticEngine -- class ini tidak import apapun dari langgraph, jadi bisa
    dites/diganti tanpa nyentuh mekanisme graph sama sekali.

    Catatan: buka koneksi sqlite sendiri ke file yang sama dengan
    checkpointer LangGraph (bukan berbagi objek koneksi). Ini aman karena
    _init_table() mengaktifkan WAL mode, yang memang didesain untuk
    beberapa koneksi terpisah membaca/menulis ke 1 file sqlite yang sama.
    """
    def __init__(self, db_path: str):
        self.db_conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_table()

    def _init_table(self):
        """Membuat tabel antrean HITL dan melakukan migrasi alter table jika kolom thread_id belum ada."""
        try:
            with self.db_conn as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS hitl_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        thread_id TEXT,
                        tools_name TEXT NOT NULL,
                        tool_args TEXT NOT NULL,
                        status TEXT DEFAULT 'MENUNGGU_PERSETUJUAN',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                cursor = conn.execute("PRAGMA table_info(hitl_queue)")
                kolom_kolom = [row[1] for row in cursor.fetchall()]

                if 'thread_id' not in kolom_kolom:
                    conn.execute("ALTER TABLE hitl_queue ADD COLUMN thread_id TEXT")
                    print("✅ [Migrasi DB] Berhasil menambahkan kolom 'thread_id' ke tabel 'hitl_queue'.")

        except Exception as e:
            print(f"⚠️ [Error DB] Gagal inisialisasi atau migrasi tabel hitl_queue: {e}")

    def record(self, thread_id: str, tool_calls: list):
        """Merekam state yang tertahan ke database untuk Watchdog Automation."""
        try:
            tools_name = ", ".join([tc["name"] for tc in tool_calls])
            tool_args = json.dumps([tc["args"] for tc in tool_calls])

            with self.db_conn as conn:
                cursor = conn.execute(
                    "SELECT id FROM hitl_queue WHERE thread_id = ? AND status = 'MENUNGGU_PERSETUJUAN'",
                    (thread_id,)
                )
                if not cursor.fetchone():
                    conn.execute(
                        "INSERT INTO hitl_queue (thread_id, tools_name, tool_args, status) VALUES (?, ?, ?, 'MENUNGGU_PERSETUJUAN')",
                        (thread_id, tools_name, tool_args)
                    )
                    print(f"📡 [Watchdog] Menambahkan {tools_name} (Thread: {thread_id}) ke antrean.")
        except Exception as e:
            print(f"⚠️ [Error DB] Gagal merekam antrean HITL: {e}")

    def update_status(self, thread_id: str, status_baru: str):
        """Memperbarui status saat user mengklik Setuju atau Batal di UI."""
        try:
            with self.db_conn as conn:
                conn.execute(
                    "UPDATE hitl_queue SET status = ? WHERE thread_id = ? AND status = 'MENUNGGU_PERSETUJUAN'",
                    (status_baru, thread_id)
                )
        except Exception:
            pass

# ==========================================
# AGENTIC ENGINE -- MURNI MERAKIT & COMPILE GRAPH
# ==========================================
class AgenticEngine:
    """
    Satu-satunya tanggung jawab: menerjemahkan `graph_config` jadi StateGraph
    ter-compile lewat rakit_graph_dari_config di atas. Tidak tahu apa-apa
    soal HITL, tidak buka koneksi DB sendiri -- kalau graph butuh
    checkpointer (mode graph utama), itu di-INJECT dari luar lewat parameter
    `checkpointer`.

    `checkpointer=None` (default) -> graph ter-compile tanpa memory
    persisten. Ini yang dipakai mode subgraph (lihat agentx_factory.py),
    MENGGANTIKAN flag `is_subgraph` yang lama -- daripada "beri tahu saya
    kamu tipe apa" (boolean flag), sekarang "beri saya apa yang kamu butuh"
    (dependency injection).
    """
    def __init__(self, state_schema: Type = AgentState, graph_config: List[Dict[str, Any]] = None,
                 checkpointer=None):
        self.state_schema = state_schema
        self.workflow = StateGraph(self.state_schema)

        # Proteksi mutlak: Tolak inisialisasi jika config kosong
        if graph_config is None:
            raise ValueError("Gagal memuat arsitektur AI! Pastikan file graph_config.py valid dan terbaca oleh Dynamic Loader.")

        self.graph_config = graph_config
        self.checkpointer = checkpointer

        # Rakit Topologi Graf (lewat mesin bersama, lihat rakit_graph_dari_config di atas)
        self.interrupt_before_nodes, self.interrupt_after_nodes = build_graph_from_config(
            self.workflow, self.graph_config
        )

        self.executor = self.workflow.compile(
            checkpointer=self.checkpointer,
            interrupt_before=self.interrupt_before_nodes,
            interrupt_after=self.interrupt_after_nodes,
        )

# ==========================================
# AGENT SESSION -- FACADE: ORKESTRASI run() + HITL
# ==========================================
class AgentSession:
    """
    Menggabungkan (KOMPOSISI, bukan mewarisi) satu `executor` hasil
    AgenticEngine dan satu `HitlQueue`, lalu mengekspos permukaan yang
    PERSIS SAMA dengan AgenticEngine versi lama: `.run(...)`. Modul lain
    (Streamlit, watchdog, dsb) yang sebelumnya manggil `engine.run(...)`
    atau method privat HITL langsung TIDAK PERLU BERUBAH -- lihat 2 method
    shim di bagian bawah class ini.
    """
    def __init__(self, executor, hitl: HitlQueue):
        self.executor = executor
        self.hitl = hitl

    def run(self, user_input: str = None, thread_id: str = "default_thread",
            is_approval: bool = False, user_role: str = "Staff") -> Dict[str, Any]:
        config = {"configurable": {"thread_id": thread_id, "user_role": user_role}, 
                  "recursion_limit": 100 # how much langgraph can execute tools
                  }
        current_state = self.executor.get_state(config)

        if current_state.next:
            if is_approval:
                self.executor.invoke(None, config=config)
            else:
                last_message = current_state.values["messages"][-1]
                inputs_to_send = []

                if hasattr(last_message, "tool_calls") and last_message.tool_calls:
                    for tc in last_message.tool_calls:
                        inputs_to_send.append(
                            ToolMessage(
                                tool_call_id=tc["id"],
                                name=tc["name"],
                                content="SYSTEM ABORT: User membatalkan aksi ini dan memberikan instruksi baru. Abaikan tool ini."
                            )
                        )

                inputs_to_send.append(HumanMessage(content=user_input))
                self.executor.invoke({"messages": inputs_to_send}, config=config)

        else:
            if not is_approval and user_input:
                self.executor.invoke({"messages": [HumanMessage(content=user_input)]}, config=config)

        # ==========================================
        # FASE 2: EVALUASI PASCA-EKSEKUSI
        # ==========================================
        state_terbaru = self.executor.get_state(config)

        if state_terbaru.next:
            pesan_terakhir = state_terbaru.values["messages"][-1]
            if hasattr(pesan_terakhir, "tool_calls") and pesan_terakhir.tool_calls:
                self.hitl.record(thread_id, pesan_terakhir.tool_calls)

        #return self.executor.get_state(config)
        return state_terbaru   # <- jangan round-trip DB lagi

    # --- Shim: nama method privat lama TETAP JALAN, tanpa modul lain diubah ---
    def _rekam_antrean_hitl(self, thread_id: str, tool_calls: list):
        return self.hitl.record(thread_id, tool_calls)

    def _update_antrean_hitl(self, thread_id: str, status_baru: str):
        return self.hitl.update_status(thread_id, status_baru)


# ==========================================
# START HELPER
# ==========================================
# [FRAMEWORK] MESIN PENERJEMAH CONFIG DEKLARATIF -> LANGGRAPH API
# ==========================================
# Tiap "type" di graph_config punya handler kecil sendiri, didaftarkan lewat
# dict _HANDLER_PER_TIPE. Nambah tipe config baru nanti = nambah 1 fungsi +
# 1 baris di dict ini -- rakit_graph_dari_config() sendiri tidak perlu
# disentuh lagi (dulu if/elif yang terus memanjang tiap ada tipe baru).
def _proses_node(workflow, item, interrupt_before, interrupt_after):
    workflow.add_node(item["name"], item["func"])
    if item.get("interrupt_before"):
        interrupt_before.append(item["name"])
    if item.get("interrupt_after"):
        interrupt_after.append(item["name"])
 
def _proses_edge(workflow, item, *_):
    workflow.add_edge(item["start"], item["end"])
 
def _proses_conditional_edge(workflow, item, *_):
    kwargs = {k: item[k] for k in ("path_map", "then") if k in item}
    workflow.add_conditional_edges(item["source"], item["router"], **kwargs)
 
def _proses_entry_point(workflow, item, *_):
    # LEGACY: API pre-START/END. Dipertahankan cuma buat jaga-jaga kalau ada
    # graph_config lama (mis. graph_config.py single-agent) yang masih pakai
    # gaya ini. Config baru sebaiknya selalu pakai START/END + "edge".
    workflow.set_entry_point(item["node"])
 
def _proses_finish_point(workflow, item, *_):
    # LEGACY, sama alasannya dengan _proses_entry_point di atas.
    workflow.set_finish_point(item["node"])
 
_HANDLER_PER_TIPE = {
    "node": _proses_node,
    "edge": _proses_edge,
    "conditional_edge": _proses_conditional_edge,
    "entry_point": _proses_entry_point,
    "finish_point": _proses_finish_point,
}

def build_graph_from_config(workflow: StateGraph, graph_config: List[Dict[str, Any]]) -> tuple:
    """
    Isi `workflow` (StateGraph kosong) dari list-of-dict config deklaratif --
    format PERSIS sama yang dipakai graph_config.py / multigraph_config.py /
    subgraph specialist di agentx_factory.py. Dipakai satu-satunya oleh
    AgenticEngine, baik mode graph utama maupun mode subgraph, supaya
    topologi SELALU didefinisikan lewat bahasa deklaratif yang sama.
 
    Return: (interrupt_before_nodes, interrupt_after_nodes).
    """
    interrupt_before: List[str] = []
    interrupt_after: List[str] = []
 
    for item in graph_config:
        handler = _HANDLER_PER_TIPE.get(item.get("type"))
        if handler is None:
            logger.warning("⚠️ PERINGATAN: Tipe konfigurasi '%s' tidak dikenali.", item.get("type"))
            continue
        handler(workflow, item, interrupt_before, interrupt_after)
 
    return interrupt_before, interrupt_after

# ==========================================
# Prepare to Frontend with Clean structure
# Gunakan cache agar Session dan MemorySaver TIDAK hancur saat UI me-reload
# BOOTSTRAP -- muat config, rakit engine, expose ke Streamlit
# ==========================================
def load_graph_config(config_name: str, config_listname: str = "GRAPH_CONFIG") -> list:
    """
    Import modul config secara dinamis dari folder agentgraph_config, lalu
    ambil variabel graph config-nya.
 
    Dulu: kegagalan di sini cuma di-print lalu lanjut dengan
    konfigurasi_aktif=None, sehingga error ASLI (ImportError/AttributeError)
    ketelan dan yang muncul ke user cuma pesan generik dari AgenticEngine
    ("Gagal memuat arsitektur AI!") tanpa jejak sebab aslinya. Sekarang gagal
    di sini langsung raise, dengan exception asli dirantai (`from e`) supaya
    kalau muncul di traceback Streamlit, akar masalahnya kelihatan.
    """
    module_path = f".agentgraph_config.{config_name}"
    try:
        modul = importlib.import_module(module_path, package=__package__)
    except ImportError as e:
        raise RuntimeError(
            f"Gagal memuat file config '{config_name}.py' dari folder agentgraph_config/."
        ) from e
 
    for nama_var in (config_listname, "DEFAULT_GRAPH_CONFIG"):
        if hasattr(modul, nama_var):
            logger.info("✅ [Auto-Load] Berhasil memuat arsitektur graf dari: agentgraph_config/%s.py", config_name)
            return getattr(modul, nama_var)
 
    raise AttributeError(
        f"File config '{config_name}.py' tidak memiliki variabel '{config_listname}' atau 'DEFAULT_GRAPH_CONFIG'."
    )

@st.cache_resource
def get_agent_engine(default_env: str = "multigraph_config", config_listname: str = "GRAPH_CONFIG") -> AgentSession:
    """
    Muat config graf -> rakit AgenticEngine (build graph) + HitlQueue
    (persistence) -> bungkus jadi satu AgentSession (facade). Permukaan
    (`engine.run(...)`) TETAP SAMA seperti sebelum refactor -- caller
    (proses_chat_agent, dsb) tidak perlu tahu ada perubahan struktur ini.
    """
    config_name = os.getenv("ACTIVE_AGENT_CONFIG", default_env)
    konfigurasi_aktif = load_graph_config(config_name, config_listname)
 
    checkpointer = SqliteSaver(sqlite3.connect(sqlite_db_path, check_same_thread=False))
    engine = AgenticEngine(graph_config=konfigurasi_aktif, checkpointer=checkpointer)
    hitl = HitlQueue(sqlite_db_path)
 
    return AgentSession(engine.executor, hitl)