"""Optional task companion; wired by the factory, does not import bot/plugin code."""
from .observer import PendampingVoyager


def buat(llm, config_path):
    from .assessment import PenilaiLLM
    from .settings import aktif
    return PendampingVoyager(lambda: aktif(config_path), PenilaiLLM(llm))


ADDON_INFO = {
    "nama": "voyager_adaptif",
    "judul": "Voyager Adaptif",
    "deskripsi": ("Per-task companion that tracks requirements and tool evidence and lets a separate LLM "
                  "assess whether the final answer covers them. Diagnostic only, it never runs tools."),
    "versi": "0.1",
}
