"""Expert-consultation ("advisor") add-on - generic SKELETON.

What this is: a hook the core can call once per agent turn (``AIBrainProcessor(penasihat_task=...)``). When
the add-on is switched on and a trigger fires (the agent asks, the same failure repeats, evidence reads loop),
it builds a redacted evidence package, asks a PROVIDER for a recommendation, validates the answer against a
fixed schema and hands the core a short advice note plus tool suggestions. At most a few consultations run per
task, and every failure degrades to "continue with the evidence you have".

What is included: the advisor loop (advisor.py), the data contract with redaction and validation
(contract.py), a provider interface with a deterministic mock and an optional generic LangChain wrapper
(provider.py), the on/off settings (settings.py), a wiring helper (wiring.py) and an example control tool
(tool_permintaan.py).

DATA EGRESS / OPT-IN - READ THIS
--------------------------------
* The add-on is OFF by default (settings.py). Nothing happens until an operator switches it on.
* Without an explicit provider the add-on does nothing: `buat()` returns an inert advisor. The bundled mock
  (`ProviderTiruan`) makes no network call and sends nothing anywhere, but it only runs when you pass it in.
* A real provider (`ProviderLLM`, or your own class) sends the evidence package - task goal, message history,
  tool results, tool catalogue - to whatever model/service you wire in. That data LEAVES this system if the
  model is remote. Redaction is best-effort pattern matching and is NOT a guarantee that secrets or personal
  or business data are removed.
* Choosing a provider is therefore a conscious host decision. This skeleton bundles no model client, handles no
  accounts or credentials and talks to no third party by itself.

Host checklist: (1) `daftarkan_tool()` from tool_permintaan.py, (2) optionally wrap the tool provider with
`wiring.lengkapi_penyedia`, (3) `AIBrainProcessor(..., penasihat_task=buat(provider=...))`, (4) switch the
add-on on (settings.tulis(aktif=True)).
"""
from .advisor import PenasihatPakar
from .contract import GalatPakar, TOOL_PERMINTAAN
from .provider import ProviderLLM, ProviderPenasihat, ProviderTiruan

# Contract with the add-on manager (listing in the management UI).
ADDON_INFO = {
    'nama': 'konsultasi_pakar',
    'judul': 'Advisor consultation (skeleton)',
    'deskripsi': 'Optional second opinion for stuck investigations: redacted evidence package, pluggable '
                 'provider (mock by default), schema-validated advice. Off by default; a real provider '
                 'sends task data to the model you choose.',
    'versi': '0.1',
}


def buat(config_path=None, provider=None):
    """Build the advisor object for ``AIBrainProcessor(penasihat_task=...)``.

    `provider`: object with ``konsultasi(paket) -> dict`` (see provider.py), for example `ProviderTiruan()`
    (deterministic mock, no network) or `ProviderLLM(model, izinkan_data_keluar=True)`. None = no provider:
    the returned advisor is inert (it never consults and never produces advice), so switching the add-on on
    without choosing a provider cannot inject made-up advice into the agent. The on/off flag and the
    consultation limit are re-read from settings at the start of each task; `config_path` is accepted for
    call compatibility and ignored (settings follow the core data dir).
    """
    from . import settings
    if provider is None:
        return PenasihatPakar(lambda: False, ProviderTiruan(), lambda: 0)
    return PenasihatPakar(lambda: settings.baca(config_path)['aktif'], provider,
                          lambda: settings.baca(config_path)['maks_konsultasi'])
