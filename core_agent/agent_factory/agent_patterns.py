import json
from langchain_core.messages import SystemMessage, HumanMessage

from ..agent_nodes import AgentState
from ..registry import FailsafeRegistry

def _signature(tc) -> str:
    """Signature stabil (nama+argumen) buat bandingkan 2 tool_calls."""
    nama = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
    args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
    return f"{nama}:{json.dumps(args, sort_keys=True, default=str)}"


def _giliran_ini(messages):
    """Generator: pesan sejak HumanMessage ASLI terakhir, terbaru ke terlama.
    Nudge internal ('[SISTEM...]') TIDAK dihitung sebagai batas giliran --
    ini satu-satunya tempat aturan itu didefinisikan, dipakai semua pengecekan
    di bawah lewat generator/comprehension, bukan for-loop manual berulang."""
    for m in reversed(messages):
        if getattr(m, "type", None) == "human" and not (getattr(m, "content", "") or "").startswith("[SISTEM"):
            return
        yield m


# ==========================================
# 0. ROUTER SPECIALIST
# ==========================================
class SpecialistRouter:
    """
    Router untuk specialist subgraph. Selain baca tool_calls (seperti
    DecisionRouter), menutup 2 lubang: (1) narasi tanpa tool call, (2) tool
    call yang mengulang persis tanpa progres. Laporan akhir yang sah (tool
    sudah pernah jalan giliran ini) tidak pernah kena keduanya.
    """

    MAX_PERCOBAAN_TANPA_TOOL = 1
    MAX_NUDGE_LOOP = 1

    def __call__(self, state: AgentState) -> str:
        messages = state.get("messages", [])
        if not messages:
            return "selesai"

        pesan_terakhir = messages[-1]
        tool_calls = getattr(pesan_terakhir, "tool_calls", None) or []
        riwayat = list(_giliran_ini(messages[:-1]))

        # Jika AI memanggil tool
        if tool_calls:
            sudah_jalan = {
                _signature(tc) for m in riwayat if getattr(m, "type", None) == "ai"
                for tc in (getattr(m, "tool_calls", None) or [])
            }
            if {_signature(tc) for tc in tool_calls} & sudah_jalan:
                return self._eskalasi(messages, "[SISTEM-LOOP]", self.MAX_NUDGE_LOOP,
                                       "cegah_loop", "gagal_loop", "tool call mengulang persis")
            return "safe"

        # Jika tool sudah pernah dipanggil sebelumnya di session ini -> Selesai
        if any(getattr(m, "type", None) == "tool" for m in riwayat):
            return "selesai"

        # OPTIONAL/PERBAIKAN: Jika AI tidak memanggil tool, tapi sudah memberikan jawaban substansial
        # (misal > 100 karakter / menjawab langsung pertanyaan umum), anggap selesai!
        konten = str(getattr(pesan_terakhir, "content", "") or "").strip()
        if len(konten) > 80 and not "akan mencari" in konten.lower():
            print("\n[Log SpecialistRouter] AI memberikan jawaban langsung yang memadai tanpa tool -> Selesai")
            return "selesai"

        # Jika jawaban kosong / hanya narasi janji mau panggil tool -> Paksa Retry
        return self._eskalasi(messages, "[SISTEM]", self.MAX_PERCOBAAN_TANPA_TOOL,
                               "paksa_retry", "gagal_tanpa_tool", "narasi tanpa tool call")

    def _eskalasi(self, messages, marker, batas, rute_retry, rute_gagal, label) -> str:
        hitung = sum(
            1 for m in _giliran_ini(messages)
            if getattr(m, "type", None) == "human" and (getattr(m, "content", "") or "").startswith(marker)
        )
        if hitung < batas:
            print(f"\n[Log SpecialistRouter] ⚠️ Terdeteksi {label} (percobaan ke-{hitung + 1}) -> {rute_retry}")
            return rute_retry
        print(f"\n[Log SpecialistRouter] Sudah {hitung}x {label}, menyerah -> {rute_gagal}")
        return rute_gagal


def _node_nudge(marker: str, teks: str):
    """Factory: node yang nempelin 1 pesan nudge (dipersist ke messages)."""
    def _node(state: AgentState) -> dict:
        return {"messages": [HumanMessage(content=f"{marker} {teks}")]}
    return _node


def _node_gagal(kode: str, default_pesan: str):
    """Factory: node fallback jujur lewat FailsafeRegistry untuk 1 kode kegagalan."""
    def _node(state: AgentState) -> dict:
        return FailsafeRegistry.get_update(kode, state, default_pesan)
    return _node


_node_paksa_retry = _node_nudge("[SISTEM]",
    "Balasanmu barusan HANYA berisi rencana tanpa tool call. JANGAN ulangi itu -- "
    "panggil tool yang relevan SEKARANG, di balasan ini juga.")

_node_cegah_loop = _node_nudge("[SISTEM-LOOP]",
    "Tool call barusan SAMA PERSIS dengan yang sudah pernah kamu jalankan di giliran "
    "ini -- JANGAN diulang. Lanjutkan ke langkah berikutnya, atau berikan laporan "
    "akhir SEKARANG tanpa tool call.")

_node_gagal_tanpa_tool = _node_gagal("tanpa_tool",
    "Maaf, saya kesulitan menyelesaikan pemeriksaan ini secara otomatis setelah "
    "beberapa kali percobaan. Coba ulangi permintaannya, atau persempit fokus pertanyaannya.")

_node_gagal_loop = _node_gagal("loop_terdeteksi",
    "Maaf, saya terjebak mengulang langkah yang sama saat memproses permintaan ini. "
    "Coba ulangi lagi permintaannya, atau persempit fokus pertanyaannya.")


# 3. SUPERVISOR: ROUTER-CLASSIFIER (BUKAN NODE TERPISAH)
# ==========================================
class SupervisorRouter:
    """1x LLM call ringan buat klasifikasi domain, dipakai langsung sebagai
    conditional edge DARI START -- tidak perlu node/field state tambahan."""

    def __init__(self, llm, prompt: str, default_route: str = "qa"):
        self.llm = llm
        self.prompt = prompt
        self.default_route = default_route
        self._label_valid = {"security", "qa", "forensics", "coder"}

    def __call__(self, state: AgentState) -> str:
        pertanyaan = next(
            (m.content for m in reversed(state.get("messages", [])) if getattr(m, "type", None) == "human"), ""
        )
        hasil = self.llm.invoke([SystemMessage(content=self.prompt), HumanMessage(content=pertanyaan)])
        label = (hasil.content or "").strip().lower()
        rute = next((k for k in self._label_valid if k in label), None)

        if rute is None:
            print(f"\n[Log Supervisor] ⚠️ Klasifikasi tidak jelas ('{label}'), fallback ke '{self.default_route}'")
            return self.default_route
        print(f"\n[Log Supervisor] '{pertanyaan[:60]}...' -> rute '{rute}'")
        return rute