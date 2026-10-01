"""Per-task consultation policy driven by a provider object.

`PenasihatPakar` is what the core calls as ``AIBrainProcessor(penasihat_task=...)``. The core invokes
``tinjau(...)`` once per agent turn (see core_agent.runtime.advice.tinjau_aman) and expects a dict with exactly
the keys ``catatan`` (checkpoint, replaced as a whole), ``pesan`` (note appended to the turn), ``tools_saran``
and ``tools_dihindari`` (tool names).

Loop per task: a trigger (the agent asked via the control tool, the same operational failure repeated, or
repeated evidence reads) builds a redacted package (`contract.buat_paket`), asks the provider and validates
the answer (`contract.validasi`). At most `maks_konsultasi` consultations run per task; failures, invalid
answers and scope changes degrade to a plain note and the normal flow continues.

Limits: this is a best-effort aid. A consultation costs time and, with a remote provider, sends data out of the
system (see provider.py). Advice is a recommendation, never evidence or permission, and the advisor makes no
guarantee that it is correct, timely or complete. No history is kept on the instance across tasks.
"""
import copy
import json

from .contract import GalatPakar, TOOL_PERMINTAAN, buat_paket, sidik, validasi

MAKS_KONSULTASI_BAWAAN = 2
MAKS_KONSULTASI_BATAS = 5


def _data(m):
    try:
        d = json.loads(m.content)
        return d if isinstance(d, dict) else {}
    except (TypeError, ValueError):
        return {}


def _pemicu(messages, processed):
    results = [m for m in messages if m.type == 'tool']
    if not results:
        return None
    # Only the newest ToolMessage batch counts; older requests are not processed again.
    batch = []
    for m in reversed(messages):
        if m.type != 'tool':
            break
        batch.append(m)
    for m in batch:
        if m.name == TOOL_PERMINTAAN and _data(m).get('status') == 'permintaan_pakar' and m.tool_call_id not in processed:
            return m.tool_call_id, 'permintaan_agent'
    if len(results) < 2:
        return None
    a, b = results[-2:]
    if a.name == TOOL_PERMINTAAN or b.name == TOOL_PERMINTAAN:
        return None
    calls = {c['id']: c for m in messages for c in getattr(m, 'tool_calls', [])}
    ca, cb = calls.get(a.tool_call_id), calls.get(b.tool_call_id)
    if not ca or not cb or (ca['name'], ca['args']) != (cb['name'], cb['args']) or a.content != b.content:
        return None
    d = _data(b)
    cakupan = d.get('cakupan') if isinstance(d.get('cakupan'), dict) else {}
    if (d.get('status') in {'running', 'pending', 'queued', 'processing', 'partial'} or
            any(d.get(k) or cakupan.get(k) for k in ('job_id', 'retry_after', 'retry_at', 'next_retry_at',
                                                    'next_cursor', 'next_page', 'halaman_berikutnya', 'has_more')) or
            d.get('pemindaian_selesai') is False or cakupan.get('pemindaian_selesai') is False):
        return None
    if getattr(b, 'artifact', None):  # A permission denial is not an operational failure.
        return None
    if getattr(b, 'status', None) == 'error' or d.get('status') in {'error', 'failed', 'gagal'}:
        key = sidik([ca['name'], ca['args'], b.content])
        return (key, 'kegagalan_identik') if key not in processed else None
    return None


def _batas(nilai):
    """Clamp a configured maximum to 0..MAKS_KONSULTASI_BATAS; invalid values fall back to the default."""
    if isinstance(nilai, bool) or not isinstance(nilai, int):
        return MAKS_KONSULTASI_BAWAAN
    return max(0, min(nilai, MAKS_KONSULTASI_BATAS))


class PenasihatPakar:
    """Advisor policy. `aktif` and `maks_konsultasi` may be plain values or zero-argument callables
    (re-read at the start of each task, so toggling takes effect for new tasks).

    `penyedia` is a provider object with ``konsultasi(paket) -> dict`` (see provider.ProviderPenasihat)
    or, for convenience, a plain callable ``paket -> dict``.
    """
    tools_kendali = (TOOL_PERMINTAAN,)

    def __init__(self, aktif, penyedia, maks_konsultasi=MAKS_KONSULTASI_BAWAAN, maks_bytes=600000):
        self._aktif, self._maks = aktif, maks_konsultasi
        self.penyedia, self.maks_bytes = penyedia, maks_bytes
        if not (callable(getattr(penyedia, 'konsultasi', None)) or callable(penyedia)):
            raise TypeError('penyedia must have a konsultasi(paket) method or be callable')

    def aktif(self):
        return bool(self._aktif() if callable(self._aktif) else self._aktif)

    def maks_konsultasi(self):
        return _batas(self._maks() if callable(self._maks) else self._maks)

    def _tanya(self, paket):
        f = getattr(self.penyedia, 'konsultasi', None)
        return f(paket) if callable(f) else self.penyedia(paket)

    def tinjau(self, previous, *, task_id, tujuan, messages, konteks, task_baru,
               katalog, kandidat, versi_katalog, catatan_task, stagnasi_bukti=None):
        scope = sidik(konteks or {})
        old = previous if isinstance(previous, dict) else {}
        beda_scope = bool(old and old.get('cakupan') != scope)
        baru = task_baru or old.get('task_id') != task_id or beda_scope
        cat = ({'task_id': task_id, 'cakupan': scope, 'aktif': self.aktif(), 'maks': self.maks_konsultasi(),
                'jumlah': 0, 'diproses': [], 'status': 'siap'} if baru else copy.deepcopy(old))
        maks = _batas(cat.setdefault('maks', self.maks_konsultasi()))
        hasil = {'catatan': cat, 'tools_saran': [], 'pesan': '', 'tools_dihindari': [TOOL_PERMINTAAN]}
        names = {t['name'] for t in katalog}
        if not cat.get('aktif') or TOOL_PERMINTAAN not in names:
            return hasil
        if beda_scope or cat.get('scope_berubah'):
            cat.update(status='history_cakupan_berubah', scope_berubah=True)
            hasil['pesan'] = '[Konsultasi pakar tidak tersedia: mulai task baru setelah perubahan cakupan.]'
            return hasil
        if cat['jumlah'] < maks:
            hasil['tools_saran'] = [TOOL_PERMINTAAN]
            hasil['tools_dihindari'] = []
        anchor = next((i for i, m in enumerate(messages) if m.type == 'human' and m.id == task_id), None)
        if anchor is None:
            cat['status'] = 'history_tidak_lengkap'
            hasil['pesan'] = '[Konsultasi pakar tidak tersedia: history awal task tidak lengkap.]'
            hasil['tools_dihindari'] = [TOOL_PERMINTAAN]
            hasil['tools_saran'] = []
            return hasil
        msgs = messages[anchor:]
        # Any change in evidence, question or catalogue makes an older recommendation stale.
        fingerprint = sidik([[(m.type, m.id, getattr(m, 'tool_call_id', None), getattr(m, 'status', None),
                              m.content, getattr(m, 'tool_calls', [])) for m in msgs],
                            versi_katalog, katalog])
        pemicu = _pemicu(msgs, cat['diproses'])
        if not pemicu and stagnasi_bukti:
            key = sidik(['bacaan_berulang', task_id, stagnasi_bukti])
            if key not in cat['diproses']:
                pemicu = key, 'bacaan_berulang'
        if pemicu and cat['jumlah'] < maks:
            key, alasan = pemicu
            cat['jumlah'] += 1
            cat['diproses'].append(key)
            cat.pop('rekomendasi', None)
            cat.update(status='gagal', pemicu=alasan)
            try:
                p = buat_paket(task_id=task_id, tujuan=tujuan, messages=msgs, katalog=katalog,
                               kandidat=kandidat, versi_katalog=versi_katalog, catatan_task=catatan_task,
                               maks_bytes=self.maks_bytes)
                r = validasi(self._tanya(p), p)
                cat.update(status='ok', rekomendasi=r, fingerprint=fingerprint)
            except GalatPakar as e:
                cat['status'] = str(e)
            except Exception:
                cat['status'] = 'gagal'   # never store provider error text: it may echo package data
        if cat['jumlah'] >= maks:
            hasil['tools_dihindari'] = [TOOL_PERMINTAAN]
            hasil['tools_saran'] = []
        r = cat.get('rekomendasi') if cat.get('fingerprint') == fingerprint else None
        if r:
            hasil['tools_saran'] += [n for n in r['tools'] if n in names and n != TOOL_PERMINTAAN]
            hasil['pesan'] = ('[Pendapat pakar: rekomendasi, bukan bukti baru atau izin tindakan. '
                              'Periksa hasil operasional sebelum menyimpulkan.]\n' + json.dumps(r, ensure_ascii=False))
        elif pemicu:
            hasil['pesan'] = ('[Konsultasi pakar belum menghasilkan rekomendasi valid: ' + cat['status'] +
                              '. Lanjut berdasarkan bukti yang tersedia; jangan mengklaim pakar sudah memeriksa.]')
        elif cat['jumlah'] < maks:
            hasil['pesan'] = ('[Pakar tersedia melalui fungsi konsultasi jika kandidat tidak cukup, '
                              'bukti bertentangan, atau arah investigasi perlu ditinjau. '
                              'Polling/pagination yang sehat tidak memerlukan konsultasi.]')
        return hasil
