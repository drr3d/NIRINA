"""Context package, redaction and validation of advisor recommendations. No service I/O.

Scope and limits (read before relying on this):

* Redaction (`samarkan`) is BEST-EFFORT pattern matching: it masks values under secret-looking dict keys and
  common ``key=value`` / header / URL-credential shapes. It cannot recognise every secret, personal datum or
  confidential business value (free text, unusual formats, encoded or split values). It reduces exposure; it
  does not guarantee that nothing sensitive is left in a package.
* Validation (`validasi`) checks shape, sizes, enum values, the snapshot echo and that referenced tool names
  and evidence ids exist. It cannot judge whether the advice is *correct*; advice is a recommendation only.
* Any provider that sends a package to a third party must be an explicit decision of the host operator.
"""
import hashlib
import json
import re

# Name of the control tool that an agent calls to ask for a consultation.
TOOL_PERMINTAAN = 'minta_konsultasi_pakar'


class GalatPakar(ValueError):
    """Fixed codes only; never put payload data in the exception message (the code may be stored)."""


def sidik(value):
    """Stable SHA-256 fingerprint of any JSON-serialisable value."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


_RAHASIA = re.compile(r'password|passwd|secret|authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|^token$', re.I)
_HEADER = re.compile(r'(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)\S+')
_COOKIE = re.compile(r'(?im)(\b(?:set-cookie|cookie|authorization)\s*[:=]\s*)[^\r\n]+')
_PASANGAN = re.compile(r'''(?i)(\b(?:password|passwd|api_key|access_token|refresh_token|token|secret)\b\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+)''')
_URL_AUTH = re.compile(r'(https?://)[^\s/@:]+:[^\s/@]+@', re.I)


def samarkan(value):
    """Return a copy of `value` with secret-looking data replaced by '[disamarkan]' (best-effort)."""
    if isinstance(value, dict):
        return {k: '[disamarkan]' if _RAHASIA.search(str(k)) else samarkan(v) for k, v in value.items()}
    if isinstance(value, list):
        return [samarkan(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(samarkan(parsed), ensure_ascii=False)
        return _URL_AUTH.sub(r'\1[disamarkan]@', _PASANGAN.sub(r'\1[disamarkan]',
                    _COOKIE.sub(r'\1[disamarkan]', _HEADER.sub(r'\1[disamarkan]', value))))
    return value


def buat_paket(*, task_id, tujuan, messages, katalog, kandidat, versi_katalog,
               catatan_task, maks_bytes=600000):
    """Build the (redacted) evidence package for one consultation.

    Raises GalatPakar('history_tidak_lengkap') when the task anchor message is missing,
    GalatPakar('history_non_teks') for non-text message content and
    GalatPakar('konteks_melebihi_batas') when the serialised package exceeds `maks_bytes`.
    """
    anchor = next((i for i, m in enumerate(messages) if m.type == 'human' and m.id == task_id), None)
    if anchor is None:
        raise GalatPakar('history_tidak_lengkap')
    history = []
    for i, m in enumerate(messages[anchor:]):
        content = m.content
        if not isinstance(content, str):
            if not isinstance(content, list) or any(not isinstance(b, dict) or b.get('type') != 'text' for b in content):
                raise GalatPakar('history_non_teks')
        history.append({'id': getattr(m, 'tool_call_id', None) or m.id or f'pesan-{i}',
                        'role': m.type, 'content': content, 'name': getattr(m, 'name', None),
                        'tool_calls': getattr(m, 'tool_calls', []), 'status': getattr(m, 'status', None)})
    p = samarkan({'tujuan': tujuan, 'history': history, 'katalog': katalog, 'kandidat': kandidat,
                  'versi_katalog': versi_katalog, 'catatan_task': catatan_task,
                  'cakupan_history': 'all messages since the task anchor; secrets redacted (best-effort)'})
    p['snapshot'] = sidik(p)
    if len(json.dumps(p, ensure_ascii=False).encode()) > maks_bytes:
        raise GalatPakar('konteks_melebihi_batas')
    return p


# Response schema a provider must return (JSON-schema style; validated by hand in `validasi`).
SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'snapshot': {'type': 'string'},
    'penilaian': {'type': 'string', 'enum': ['lanjut', 'tunggu', 'perlu_bukti', 'perlu_tool', 'terhalang', 'belum_pasti']},
    'referensi_bukti': {'type': 'array', 'items': {'type': 'string'}},
    'kebutuhan_terbuka': {'type': 'array', 'items': {'type': 'string'}},
    'tools': {'type': 'array', 'items': {'type': 'string'}},
    'langkah': {'type': 'string'}, 'batas_kesimpulan': {'type': 'string'}}}
SCHEMA['required'] = list(SCHEMA['properties'])


def validasi(r, p):
    """Validate provider response `r` against package `p`; return a redacted, normalised copy.

    Raises GalatPakar with a fixed code on any violation (wrong keys/types, oversize strings or lists,
    snapshot mismatch, unknown assessment, unknown tool names, unknown evidence ids).
    """
    if not isinstance(r, dict) or set(r) != set(SCHEMA['properties']):
        raise GalatPakar('respons_tidak_valid')
    for k in ('snapshot', 'penilaian', 'langkah', 'batas_kesimpulan'):
        if not isinstance(r[k], str) or len(r[k]) > 3000:
            raise GalatPakar('respons_tidak_valid')
    for k in ('referensi_bukti', 'kebutuhan_terbuka', 'tools'):
        if not isinstance(r[k], list) or len(r[k]) > 24 or any(not isinstance(x, str) or len(x) > 600 for x in r[k]):
            raise GalatPakar('respons_tidak_valid')
    if r['snapshot'] != p['snapshot'] or r['penilaian'] not in SCHEMA['properties']['penilaian']['enum']:
        raise GalatPakar('snapshot_atau_penilaian_tidak_valid')
    nama = {t['name'] for t in p['katalog']}
    bukti = {m['id'] for m in p['history']}
    if not set(r['tools']) <= nama or not set(r['referensi_bukti']) <= bukti:
        raise GalatPakar('referensi_tidak_valid')
    return {**samarkan(r), 'tools': list(dict.fromkeys(r['tools']))[:5]}
