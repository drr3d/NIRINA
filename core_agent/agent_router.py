# ==========================================
# File: core_agent/agent_router.py
# ==========================================
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Any, Dict, Union, Optional

from langchain_core.messages import SystemMessage, HumanMessage

from .agent_nodes import AgentState

# Gunakan Logger standar Python untuk Framework Level
logger = logging.getLogger("FrameworkRouter")

# ==========================================
# 1. DATACLASS KONFIGURASI ROUTER
# ==========================================
nofinal_kw = ["akan mencari", "akan memeriksa", "let me check"] # mungkin harus di taro di file config.json
@dataclass
class RouterConfig:
    """Konfigurasi terpusat untuk Router Framework."""
    enable_parallel: bool = False
    max_retry_kosong: int = 2
    max_tool_repeat: int = 3
    max_percobaan_tanpa_tool: int = 1
    max_nudge_loop: int = 1
    min_content_length_substantive: int = 80
    non_final_keywords: List[str] = field(default_factory=lambda: nofinal_kw)
    fallback_route: str = "safe"
    special_routing: Dict[str, str] = field(default_factory=dict)
    category_priority: List[str] = field(default_factory=list)


# ==========================================
# 2. HELPER UTILITAS ROUTER
# ==========================================
def _signature(tc) -> str:
    """Signature stabil (nama+argumen) buat bandingkan 2 tool_calls."""
    nama = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
    args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
    return f"{nama}:{json.dumps(args, sort_keys=True, default=str)}"


def _giliran_ini(messages):
    """Generator: pesan sejak HumanMessage ASLI terakhir, terbaru ke terlama."""
    for m in reversed(messages):
        if getattr(m, "type", None) == "human" and not (getattr(m, "content", "") or "").startswith("[SISTEM"):
            return
        yield m


# ==========================================
# 3. BASE ABSTRACT ROUTER
# ==========================================
class BaseRouter(ABC):
    """Interface dasar untuk semua Router di dalam Framework."""

    @abstractmethod
    def __call__(self, state: AgentState) -> Union[str, List[str]]:
        """Entry point wajib untuk LangGraph Conditional Edge."""
        pass


# ==========================================
# 4. DECISION ROUTER (MAIN AGENT)
# ==========================================
class DecisionRouter(BaseRouter):
    """
    Router Dinamis untuk Main Agent dengan arsitektur Guardrail Pipeline.
    """

    def __init__(
        self, 
        tools_by_category: Dict[str, List[Any]], 
        config: Optional[RouterConfig] = None,
        # Untuk backward compatibility jika dipanggil tanpa RouterConfig:
        fallback_route: str = "safe",
        special_routing: Optional[Dict[str, str]] = None,
        category_priority: Optional[List[str]] = None,
    ):
        # Inisialisasi Config (bisa via object RouterConfig atau individual args)
        self.config = config or RouterConfig(
            fallback_route=fallback_route,
            special_routing=special_routing or {},
            category_priority=category_priority or []
        )
        
        # Build Lookup Table dinamis
        self.tool_to_category: Dict[str, str] = {}
        for category, tools in tools_by_category.items():
            for t in tools:
                nama_tool = getattr(t, 'name', t)
                self.tool_to_category[nama_tool] = category

    # --- PIPELINE GUARDRAILS ---
    def _check_invalid_json(self, invalid_calls, tool_calls) -> Optional[str]:
        if invalid_calls and not tool_calls:
            logger.warning("[DecisionRouter] ❌ AI gagal memformat Tool JSON -> retry_kosong")
            return "retry_kosong"
        return None

    def _check_tool_loop(self, tool_repeat_count: int, tool_calls: list) -> Optional[str]:
        if tool_repeat_count >= self.config.max_tool_repeat:
            nama_tools = ", ".join(tc.get('name') if isinstance(tc, dict) else getattr(tc, 'name', '') for tc in tool_calls)
            logger.error(f"[DecisionRouter] 🔁 Loop terdeteksi pada tool [{nama_tools}] ({tool_repeat_count}x) -> gagal_looping")
            return "gagal_looping"
        return None

    def _resolve_tool_routes(self, tool_calls: list) -> Union[str, List[str]]:
        kategori_ditemukan = set()
        daftar_nama_tool = []

        for tc in tool_calls:
            nama_tool = tc.get('name') if isinstance(tc, dict) else getattr(tc, 'name', '')
            daftar_nama_tool.append(nama_tool)

            # Resolve Category via Intercept or Lookup Table
            kategori = self.config.special_routing.get(
                nama_tool, 
                self.tool_to_category.get(nama_tool, self.config.fallback_route)
            )
            kategori_ditemukan.add(kategori)

        nama_tools_log = ", ".join(daftar_nama_tool)
        kategori_khusus = [k for k in kategori_ditemukan if k != self.config.fallback_route]

        # Mode Parallel Execution
        if self.config.enable_parallel:
            rute_final = list(kategori_ditemukan)
            rute_final = rute_final if len(rute_final) > 1 else rute_final[0]
            logger.info(f"[DecisionRouter] ⚡ Parallel Mode: [{nama_tools_log}] -> Fan-out: {rute_final}")
            return rute_final

        # Mode Sequential Priority
        kategori_tujuan = self.config.fallback_route
        if kategori_khusus:
            # Match priority order
            for prioritas in self.config.category_priority:
                if prioritas in kategori_khusus:
                    kategori_tujuan = prioritas
                    break
            else:
                kategori_tujuan = kategori_khusus[0]

        logger.info(f"[DecisionRouter] 🚦 Sequential Mode: [{nama_tools_log}] -> Rute: '{kategori_tujuan}'")
        return kategori_tujuan

    def _check_empty_response(self, konten: str, revision_count: int) -> str:
        if revision_count < self.config.max_retry_kosong:
            logger.warning(f"[DecisionRouter] ⚠️ Respons kosong (retry ke-{revision_count + 1}) -> retry_kosong")
            return "retry_kosong"
        logger.error(f"[DecisionRouter] ❌ Respons kosong menetap setelah retry -> gagal_kosong")
        return "gagal_kosong"

    def __call__(self, state: AgentState) -> Union[str, List[str]]:
        if not state.get("messages"):
            return "selesai"

        pesan_terakhir = state["messages"][-1]
        revision_count = state.get("revision_count", 0)
        tool_repeat_count = state.get("tool_repeat_count", 0)

        tool_calls = getattr(pesan_terakhir, "tool_calls", [])
        invalid_calls = getattr(pesan_terakhir, "invalid_tool_calls", [])

        # 1. Guardrail JSON Cacat
        rute_err = self._check_invalid_json(invalid_calls, tool_calls)
        if rute_err: return rute_err

        # 2. Jika ada Tool Calls
        if tool_calls:
            rute_loop = self._check_tool_loop(tool_repeat_count, tool_calls)
            if rute_loop: return rute_loop
            
            return self._resolve_tool_routes(tool_calls)

        # 3. Guardrail Respons Kosong
        konten = getattr(pesan_terakhir, "content", "") or ""
        if not konten.strip():
            return self._check_empty_response(konten, revision_count)

        logger.info("[DecisionRouter] Draf selesai -> selesai")
        return "selesai"


# ==========================================
# 5. SPECIALIST ROUTER (SUB-AGENT)
# ==========================================
class SpecialistRouter(BaseRouter):
    """
    Router Pengawal Eksekusi Subgraph dengan Rule Heuristic yang Fleksibel.
    """

    def __init__(self, config: Optional[RouterConfig] = None):
        self.config = config or RouterConfig()

    def _is_substantive_response(self, konten: str) -> bool:
        """Pengecekan dinamis apakah jawaban AI substansial tanpa panggil tool."""
        konten_clean = konten.strip().lower()
        
        # Panjang cukup & tidak mengandung kata-kata penundaan/janji
        panjang_cukup = len(konten_clean) >= self.config.min_content_length_substantive
        ada_janji = any(kw in konten_clean for kw in self.config.non_final_keywords)

        return panjang_cukup and not ada_janji

    def __call__(self, state: AgentState) -> str:
        messages = state.get("messages", [])
        if not messages:
            return "selesai"

        pesan_terakhir = messages[-1]
        tool_calls = getattr(pesan_terakhir, "tool_calls", None) or []
        riwayat = list(_giliran_ini(messages[:-1]))

        # 1. Jika AI memanggil tool
        if tool_calls:
            sudah_jalan = {
                _signature(tc) for m in riwayat if getattr(m, "type", None) == "ai"
                for tc in (getattr(m, "tool_calls", None) or [])
            }
            if {_signature(tc) for tc in tool_calls} & sudah_jalan:
                return self._eskalasi(messages, "[SISTEM-LOOP]", self.config.max_nudge_loop,
                                       "cegah_loop", "gagal_loop", "tool call mengulang persis")
            return "safe"

        # 2. Jika tool sudah pernah dipanggil sebelumnya di turn ini -> Selesai
        if any(getattr(m, "type", None) == "tool" for m in riwayat):
            return "selesai"

        # 3. Deteksi Jawaban Substantif tanpa tool -> Selesai
        konten = str(getattr(pesan_terakhir, "content", "") or "")
        if self._is_substantive_response(konten):
            logger.info("[SpecialistRouter] AI memberikan jawaban langsung yang memadai -> selesai")
            return "selesai"

        # 4. Jawaban Kosong / Narasi tanpa tool -> Paksa Retry
        return self._eskalasi(messages, "[SISTEM]", self.config.max_percobaan_tanpa_tool,
                               "paksa_retry", "gagal_tanpa_tool", "narasi tanpa tool call")

    def _eskalasi(self, messages, marker, batas, rute_retry, rute_gagal, label) -> str:
        hitung = sum(
            1 for m in _giliran_ini(messages)
            if getattr(m, "type", None) == "human" and (getattr(m, "content", "") or "").startswith(marker)
        )
        if hitung < batas:
            logger.warning(f"[SpecialistRouter] ⚠️ {label} (percobaan ke-{hitung + 1}) -> {rute_retry}")
            return rute_retry
        logger.error(f"[SpecialistRouter] {hitung}x {label}, menyerah -> {rute_gagal}")
        return rute_gagal

# ============================================
# --- 3. SUPERVISOR ROUTER (MULTI-AGENT INTENT CLASSIFIER) ---
# ============================================
class SupervisorRouter(BaseRouter):
    """
    Router Klasifikasi Intent (LLM-based) untuk Arsitektur Multi-Agent.
    Bertindak sebagai pimpinan di node START untuk mengarahkan prompt user 
    ke Agen / Subgraph Spesialis yang sesuai.
    """

    def __init__(
        self, 
        llm: Any, 
        prompt: str, 
        valid_labels: Union[List[str], set], 
        default_route: str = "qa",
        logger_instance=None
    ):
        self.llm = llm
        self.prompt = prompt
        # INJEKSI DINAMIS: User/Graph Config bebas menentukan label agen apapun!
        self.valid_labels = set(valid_labels)
        self.default_route = default_route
        self.logger = logger_instance or logger

    def __call__(self, state: AgentState) -> str:
        # Ambil pertanyaan pesan manusia (HumanMessage) terakhir
        pertanyaan = next(
            (m.content for m in reversed(state.get("messages", [])) if getattr(m, "type", None) == "human"), ""
        )
        
        if not pertanyaan:
            self.logger.warning("[SupervisorRouter] Tidak ada pesan HumanMessage terdeteksi, fallback ke default.")
            return self.default_route

        try:
            # Panggil LLM ringan untuk klasifikasi cepat
            hasil = self.llm.invoke([
                SystemMessage(content=self.prompt), 
                HumanMessage(content=pertanyaan)
            ])
            label = (hasil.content or "").strip().lower()
            
            # Cari apakah ada label valid yang cocok di dalam respons LLM
            rute = next((k for k in self.valid_labels if k in label), None)

            if rute is None:
                self.logger.warning(f"[SupervisorRouter] ⚠️ Klasifikasi tidak jelas ('{label}'), fallback ke '{self.default_route}'")
                return self.default_route
            
            self.logger.info(f"[SupervisorRouter] 🎯 Input User: '{pertanyaan[:50]}...' -> Di-route ke Agen: '{rute}'")
            return rute

        except Exception as e:
            self.logger.error(f"[SupervisorRouter] ❌ Gagal melakukan klasifikasi via LLM: {e}")
            return self.default_route