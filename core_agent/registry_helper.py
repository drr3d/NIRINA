from langchain_core.messages import AIMessage
class FailsafeRegistry:
    _handlers = {}

    KODE_KOSONG = "kosong"

    @classmethod
    def register(cls, kode: str):
        """Decorator: daftarkan handler(state) -> str|dict untuk satu kode failsafe."""
        def decorator(func):
            cls._handlers[kode] = func
            return func
        return decorator

    @classmethod
    def get_update(cls, kode: str, state, default_pesan: str) -> dict:
        """
        Dipanggil dari node core. Mengembalikan dict update state siap pakai.
        - Tidak ada handler terdaftar utk `kode`  -> pakai default_pesan.
        - Handler terdaftar & return str          -> dibungkus jadi AIMessage.
        - Handler terdaftar & return dict          -> dipakai apa adanya (kontrol penuh).
        - Handler error / return kosong            -> fallback ke default_pesan
          (supaya plugin yang ditulis asal-asalan tidak menjatuhkan seluruh graf).
        """
        revision_count = state.get("revision_count", 0) if hasattr(state, "get") else 0
        default_update = {
            "messages": [AIMessage(content=default_pesan)],
            "revision_count": -revision_count,
        }

        handler = cls._handlers.get(kode)
        if handler is None:
            return default_update

        try:
            hasil = handler(state)
            if isinstance(hasil, dict):
                return hasil
            if isinstance(hasil, str) and hasil.strip():
                return {
                    "messages": [AIMessage(content=hasil)],
                    "revision_count": -revision_count,
                }
            return default_update
        except Exception as e:
            print(f"⚠️ [FailsafeRegistry] Handler custom untuk kode '{kode}' error, pakai default. Detail: {e}")
            return default_update

class GuardrailRegistry:
    _guardrails = {}

    @classmethod
    def register(cls, kategori: str):
        """Decorator: daftarkan validator(nama_tool, args) -> str|None untuk satu kategori."""
        def decorator(func):
            cls._guardrails[kategori] = func
            return func
        return decorator

    @classmethod
    def check(cls, kategori: str, nama_tool: str, args: dict) -> str | None:
        """
        Dipanggil dari node core sebelum eksekusi tool. Mengembalikan None
        kalau lolos (atau tidak ada guardrail terdaftar untuk kategori ini),
        atau string alasan penolakan kalau tool call ini harus diblokir.
        Error di dalam handler custom tidak menjatuhkan graf -- dianggap
        lolos dengan warning ke log (sama seperti filosofi FailsafeRegistry).
        """
        handler = cls._guardrails.get(kategori)
        if handler is None:
            return None
        try:
            return handler(nama_tool, args)
        except Exception as e:
            print(f"⚠️ [GuardrailRegistry] Handler validasi kategori '{kategori}' error, tool LOLOS default. Detail: {e}")
            return None


class SmokeTestRegistry:
    _tests = {}
 
    @classmethod
    def register(cls, nama_file: str):
        """Decorator: daftarkan fungsi tes(modul) -> None (lempar exception kalau gagal)."""
        def decorator(func):
            cls._tests[nama_file] = func
            return func
        return decorator
 
    @classmethod
    def get_test(cls, nama_file: str):
        """Ambil fungsi tes terdaftar untuk nama_file, atau None kalau belum ada."""
        return cls._tests.get(nama_file)