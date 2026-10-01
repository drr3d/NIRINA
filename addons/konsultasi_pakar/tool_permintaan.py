"""Example control tool: lets the agent ask for a consultation. Opt-in registration (nothing at import time).

The advisor (advisor.py) only offers consultations when a tool named `minta_konsultasi_pakar` exists in the
tool catalogue; that name is reserved by the core (core_agent/tools/catalog.py) for trusted sources, which
includes tools registered through the built-in `ToolRegistry`. The tool itself does nothing but record the
request: the advisor sees its result on the next agent turn and builds the package then.

How a host registers it (only when it also passes ``penasihat_task=buat(...)`` to the brain, otherwise the
agent could call a tool that never gets an answer):

    from addons.konsultasi_pakar.tool_permintaan import daftarkan_tool
    daftarkan_tool()          # registers once, idempotent

and, so that domain agents also receive the control tool when the advisor suggests it, wrap the per-turn tool
provider (see wiring.py):

    penyedia_tools = lengkapi_penyedia(ToolRegistry.penyedia_agen(domain, ambang=...))
"""
import json

from .contract import TOOL_PERMINTAAN

_tool = None


def daftarkan_tool():
    """Register the control tool with the public ToolRegistry (once) and return it."""
    global _tool
    if _tool is not None:
        return _tool
    from core_agent.tools.registry import ToolRegistry

    @ToolRegistry.register(category='umum', sensitive=False,
                           intents='expert opinion advisor stuck investigation conflicting evidence extra check')
    def minta_konsultasi_pakar(pertanyaan: str) -> str:
        """Ask for an expert opinion when an investigation is stuck, evidence conflicts, or the next check is unclear.
        History and catalogue are attached by the system on the next turn. Not for polling that is still running.
        """
        if not isinstance(pertanyaan, str) or not pertanyaan.strip() or len(pertanyaan) > 2000:
            return json.dumps({'status': 'error', 'pesan': 'Pertanyaan harus 1-2000 karakter.'})
        return json.dumps({'status': 'permintaan_pakar', 'pertanyaan': pertanyaan,
                           'pesan': 'Permintaan dicatat; tunggu hasil konsultasi pada konteks giliran berikutnya.'},
                          ensure_ascii=False)

    assert minta_konsultasi_pakar.name == TOOL_PERMINTAAN
    _tool = minta_konsultasi_pakar
    return _tool
