"""Per-turn tool provider wrapper: gives domain agents the control tool when the catalogue has it.

Works with the public core: a provider is ``fn(snap) -> PerangkatGiliran`` (or a list of tools), as produced by
``ToolRegistry.penyedia_agen(...)``. Domain agents normally only see their own domain's tools; the advisor
suggests the control tool through ``tools_saran``, which can only be bound when it is in the agent's tool set.
This wrapper appends it (from the same snapshot) when it exists and its source is trusted and bound.
"""
from dataclasses import replace

from .contract import TOOL_PERMINTAAN


def lengkapi_penyedia(penyedia):
    def ambil(snap):
        p = penyedia(snap)
        t = snap.tool(TOOL_PERMINTAAN) if snap is not None and hasattr(snap, 'tool') else None
        meta = snap.meta_tool(TOOL_PERMINTAAN) if t is not None else None
        if t is None or meta is None or not meta.tepercaya or not meta.terikat:
            return p
        daftar = p if isinstance(p, (list, tuple)) else p.tools
        if any(x.name == t.name for x in daftar):
            return p
        # Domain retrieval stays intact; the advisor offers the control tool through tools_saran.
        if isinstance(p, (list, tuple)):
            return [*p, t]
        return replace(p, tools=(*p.tools, t))
    return ambil
