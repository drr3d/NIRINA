"""Per-task state: bounded facts + a separate assessment, no tool execution."""
import copy
import hashlib
import json

from langchain_core.messages import HumanMessage
from core_agent.llm.format import teks_dari_konten
from core_agent.runtime.state import NAMA_TOOL_META_BUKAN_BAGIAN_TRACE
from core_agent.tools.unduhan import tanpa_path

MAKS_BUKTI = 24
MAKS_HASIL = 1600
MAKS_TUJUAN = 6000


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _cakupan(konteks):
    # Identitas tidak diberikan ke penilai atau log; perubahan cakupan memulai catatan baru.
    return _hash({k: (konteks or {}).get(k) for k in ('namespace', 'mode', 'client_id', 'user_id')})


def _status(message, teks):
    if getattr(message, 'status', None) == 'error':
        return 'error'
    try:
        data = json.loads(teks)
    except (ValueError, TypeError):
        return 'unknown'
    if not isinstance(data, dict):
        return 'unknown'
    status = str(data.get('status', '')).lower()
    code = data.get('status_code')
    if (status in {'error', 'failed', 'gagal', 'ditolak', 'input_invalid', 'not_found', 'unavailable', 'ambigu'} or data.get('success') is False
            or data.get('error') or data.get('errMsg')
            or (type(code) is int and code >= 400)):
        return 'error'
    cakupan = data.get('cakupan') if isinstance(data.get('cakupan'), dict) else {}
    if (status in {'pending', 'running', 'queued', 'processing', 'menunggu', 'partial'}
            or data.get('has_more') is True or data.get('halaman_berikutnya')
            or data.get('next_cursor') or data.get('next_page') or data.get('pemindaian_selesai') is False
            or cakupan.get('halaman_berikutnya') or cakupan.get('pemindaian_selesai') is False):
        return 'pending'
    if status and status not in {'ok', 'success', 'berhasil', 'complete', 'completed'}:
        return 'unknown'
    return 'success'


class PendampingVoyager:
    """Objek bersama hanya menyimpan dependency; seluruh data task ada di catatan."""
    def __init__(self, aktif, penilai):
        self.aktif = aktif
        self.penilai = penilai

    def _nilai(self, tahap, data):
        try:
            hasil = self.penilai(tahap, copy.deepcopy(data))
            if not isinstance(hasil, dict):
                raise ValueError('Format penilai tidak valid')
            return hasil, None
        except Exception as e:
            return {}, type(e).__name__

    def siapkan(self, previous, *, task_id, tujuan, messages, konteks, task_baru):
        scope = _cakupan(konteks)
        lama = previous if isinstance(previous, dict) and previous.get('versi') == 1 else None
        ganti_cakupan = lama is not None and lama.get('cakupan') != scope
        baru = (task_baru or lama is None or lama.get('task_id') != task_id
                or lama.get('cakupan') != scope)
        if baru:
            cat = {'versi': 1, 'task_id': task_id, 'cakupan': scope, 'aktif': bool(self.aktif())}
        else:
            cat = copy.deepcopy(lama)
        if not cat['aktif']:
            return cat
        tujuan = str(tujuan or '')
        if baru:
            nilai, error = self._nilai('kebutuhan', {'tujuan': tujuan[:MAKS_TUJUAN]})
            needs = nilai.get('kebutuhan')
            sah = (isinstance(needs, list) and 0 < len(needs) <= 8
                   and all(isinstance(n, str) and 0 < len(n.strip()) <= 600 for n in needs))
            needs = needs if sah else [tujuan[:600] or 'Permintaan pengguna belum jelas']
            cat.update(tujuan=tujuan[:MAKS_TUJUAN], tujuan_hash=_hash(tujuan),
                       kebutuhan=[{'id': f'K{i + 1}', 'teks': n, 'status': 'belum_diketahui',
                                   'bukti': []} for i, n in enumerate(needs)],
                       bukti=[], memori=[], final_count=0,
                       cakupan_terpotong=len(tujuan) > MAKS_TUJUAN or (not sah and len(tujuan) > 600),
                       pemeriksaan={'status': 'belum_dinilai', 'sumber': 'penilaian_llm'},
                       perumusan={'sumber': 'penilaian_llm' if sah else 'permintaan_asli',
                                  'error': error})
            # Bila state salah dipakai pada cakupan berbeda, jangan mengambil hasil lama
            # kembali pada giliran berikutnya. Simpan hash ID saja, tanpa payload cakupan lama.
            cat['abaikan_hasil'] = ([_hash(getattr(m, 'tool_call_id', None)) for m in messages or []
                                    if getattr(m, 'type', None) == 'tool'] if ganti_cakupan else [])
        elif cat['tujuan_hash'] != _hash(tujuan):
            # Perluasan topik tidak menghapus kebutuhan lama atau menghabiskan panggilan perumusan lagi.
            tambahan = tujuan[len(cat['tujuan']):].lstrip(' |') if tujuan.startswith(cat['tujuan']) else tujuan
            cat['tujuan'] = tujuan[:MAKS_TUJUAN]
            cat['tujuan_hash'] = _hash(tujuan)
            cat['cakupan_terpotong'] |= len(tujuan) > MAKS_TUJUAN or len(tambahan) > 600
            if len(cat['kebutuhan']) < 12:
                cat['kebutuhan'].append({'id': f"K{len(cat['kebutuhan']) + 1}",
                                        'teks': tambahan[:600], 'dipotong': len(tambahan) > 600,
                                        'status': 'belum_diketahui', 'bukti': []})
            else:
                cat['cakupan_terpotong'] = True
            cat['pemeriksaan'] = {'status': 'belum_dinilai', 'sumber': 'penilaian_llm'}
        # Pada task baru/checkpoint lama, hanya pesan sejak anchor yang sah boleh diambil.
        msgs = list(messages or [])
        anchor = next((i for i, m in enumerate(msgs) if task_id is not None
                       and getattr(m, 'type', None) == 'human' and getattr(m, 'id', None) == task_id), None)
        if baru:
            msgs = msgs[anchor + 1:] if anchor is not None else []
        elif anchor is not None:
            msgs = msgs[anchor + 1:]
        else:
            # Cleaner mungkin membuang anchor; baca hanya batch hasil paling akhir.
            ekor = []
            for m in reversed(msgs):
                if getattr(m, 'type', None) != 'tool':
                    break
                ekor.append(m)
            msgs = list(reversed(ekor))
        known = {b['id'] for b in cat['bukti']}
        for m in msgs:
            if getattr(m, 'type', None) != 'tool' or getattr(m, 'name', None) in NAMA_TOOL_META_BUKAN_BAGIAN_TRACE:
                continue
            ident = getattr(m, 'tool_call_id', None)
            if not ident or ident in known or _hash(ident) in cat.get('abaikan_hasil', []):
                continue
            if len(cat['bukti']) >= MAKS_BUKTI:
                cat['cakupan_terpotong'] = True
                break
            # Hasil tool download: path file server tidak dikirim ke penilai.
            teks = teks_dari_konten(tanpa_path(m).content)
            cat['bukti'].append({'id': ident, 'tool': getattr(m, 'name', None),
                                 'status': _status(m, teks), 'hasil': teks[:MAKS_HASIL],
                                 'dipotong': len(teks) > MAKS_HASIL})
            known.add(ident)
            cat['pemeriksaan'] = {'status': 'belum_dinilai', 'sumber': 'penilaian_llm'}
        return cat

    def konteks(self, catatan, skills):
        cat = copy.deepcopy(catatan)
        if not cat or not cat.get('aktif'):
            return cat, []
        cat['memori'] = [{'id': f'M{i + 1}', 'deskripsi': str(s.get('deskripsi', ''))[:500],
                          'skor_historis': s.get('skor'), 'status': 'belum_diketahui',
                          'dipotong': len(str(s.get('deskripsi', ''))) > 500}
                         for i, s in enumerate((skills or [])[:3])]
        # Bukti mentah sudah berada di ToolMessage; tidak menggandakannya ke prompt utama.
        data = {'kebutuhan': cat['kebutuhan'],
                'hasil_tool': [{k: b[k] for k in ('id', 'tool', 'status', 'dipotong')} for b in cat['bukti']],
                'cakupan_terpotong': cat['cakupan_terpotong']}
        pesan = HumanMessage(content='[INFO SISTEM: PEMERIKSAAN TASK]\n'
            'Catatan berikut adalah data dan penilaian sementara, bukan instruksi tambahan pengguna. '
            'Periksa kebutuhan terhadap hasil tool sebelum menjawab. Success tool bukan bukti task selesai. '
            'Skor pengalaman lama tidak membuktikan kecocokan sekarang. Jika belum cukup, gunakan tool '
            'yang diizinkan atau jelaskan batas hasil. Jangan menganggap urutan lama wajib diikuti.\n'
            + json.dumps(data, ensure_ascii=False))
        return cat, [pesan]

    def selesai(self, catatan, response):
        cat = copy.deepcopy(catatan)
        if (not cat or not cat.get('aktif') or getattr(response, 'tool_calls', None)
                or not teks_dari_konten(response.content).strip()):
            return cat
        if cat['final_count'] >= 2:
            cat['pemeriksaan'] = {'status': 'batas_penilaian', 'sumber': 'penilaian_llm'}
            return cat
        cat['final_count'] += 1
        draf = teks_dari_konten(response.content)
        hasil, error = self._nilai('akhir', {'tujuan': cat['tujuan'], 'kebutuhan': cat['kebutuhan'],
                                           'bukti': cat['bukti'], 'memori': cat['memori'],
                                           'draf': draf[:6000]})
        rows = hasil.get('kebutuhan', [])
        rows = rows if isinstance(rows, list) else []
        bukti_sah = {b['id'] for b in cat['bukti'] if b['status'] == 'success' and not b['dipotong']}
        for need in cat['kebutuhan']:
            cocok = [r for r in rows if isinstance(r, dict) and r.get('id') == need['id']]
            r = cocok[0] if len(cocok) == 1 else {}
            status = r.get('status', 'belum_diketahui')
            refs = r.get('bukti', [])
            refs_sah = isinstance(refs, list) and all(isinstance(x, str) and x in bukti_sah for x in refs)
            if (not isinstance(status, str) or status not in {'terpenuhi', 'terbuka', 'belum_diketahui'}
                    or (status == 'terpenuhi' and not (refs_sah and refs))):
                status = 'belum_diketahui'
            need.update(status=status, bukti=refs if refs_sah else [],
                        alasan=str(r.get('alasan', ''))[:300], sumber='penilaian_llm')
        memori = hasil.get('kecocokan_memori', [])
        for m in cat['memori']:
            r = next((r for r in memori if isinstance(r, dict) and r.get('id') == m['id']), {}) if isinstance(memori, list) else {}
            status = r.get('status')
            m.update(status=status if not m.get('dipotong') and isinstance(status, str)
                     and status in {'cocok', 'sebagian', 'bertentangan'} else 'belum_diketahui',
                     alasan=str(r.get('alasan', ''))[:300], sumber='penilaian_llm')
        lengkap = (all(k['status'] == 'terpenuhi' for k in cat['kebutuhan'])
                   and not cat['cakupan_terpotong'] and len(draf) <= 6000
                   and not any(b['dipotong'] for b in cat['bukti']))
        cat['pemeriksaan'] = {'status': 'dinilai_lengkap' if lengkap else 'belum_lengkap',
                             'sumber': 'penilaian_llm', 'error': error}
        if error:
            cat['pemeriksaan']['status'] = 'gagal_dinilai'
        return cat
