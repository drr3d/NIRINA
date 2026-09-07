from langchain_core.messages import AIMessage
class FailsafeRegistry:
    """
    Registry untuk skenario GAGAL/FAILSAFE di dalam graf (mis. AI balik dengan
    respons kosong berkali-kali). Polanya sengaja dibuat identik dengan
    ToolRegistry & ToolFormatterRegistry: kontributor cukup pasang decorator
    di file plugin masing-masing (folder `plugins/`, ke-scan otomatis oleh
    AUTO-DISCOVERY di agent_factory.py) -- TIDAK PERLU membuka atau mengubah
    core system (agent_nodes.py) sama sekali untuk mengganti perilaku failsafe.

    Setiap skenario diberi `kode` unik (mis. "kosong" untuk kasus respons AI
    kosong berulang). Kalau tidak ada handler custom terdaftar untuk kode itu
    -- atau handler-nya error -- sistem otomatis jatuh ke default bawaan yang
    dikirim oleh si pemanggil (node core), jadi node core TETAP JALAN NORMAL
    walau belum ada satupun plugin failsafe terpasang.

    Cara pakai di file plugin:

        from core_agent.registry import FailsafeRegistry

        @FailsafeRegistry.register("kosong")
        def pesan_kosong_versi_saya(state) -> str:
            return "Pesan custom kamu di sini, boleh baca `state` juga."

    Untuk kontrol penuh (bukan cuma ganti teks -- misal mau nambah field state
    lain, trigger notifikasi, dst), handler boleh return dict langsung; dict
    itu dipakai APA ADANYA sebagai update state LangGraph:

        @FailsafeRegistry.register("kosong")
        def handler_lanjutan(state) -> dict:
            return {"messages": [...], "revision_count": 0, "pending_tasks": ""}
    """
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
    """
    Registry untuk validasi ARGUMEN tool call SEBELUM tool-nya benar-benar
    dieksekusi -- didaftarkan PER KATEGORI (mis. "pentest"), bukan per tool
    satu-satu, supaya proteksi konsisten untuk semua tool dalam kategori yang
    sama tanpa perlu duplikasi validasi di tiap file tool.

    Pola sengaja dibuat identik dengan FailsafeRegistry & SmokeTestRegistry:
    kontributor cukup pasang decorator di file plugin masing-masing --
    TIDAK PERLU membuka atau mengubah core (agent_router.py/agent_nodes.py)
    untuk menambah/mengganti aturan validasi.

    Cara pakai di file plugin:

        from core_agent.registry import GuardrailRegistry

        @GuardrailRegistry.register("pentest")
        def validasi_pentest(nama_tool: str, args: dict) -> str | None:
            # return None kalau lolos, atau STRING ALASAN PENOLAKAN kalau ditolak.
            # String itu yang akan dikirim balik ke LLM sebagai ToolMessage,
            # menggantikan eksekusi tool yang sesungguhnya.
            if "DROP TABLE" in str(args).upper():
                return f"Tool '{nama_tool}' ditolak: argumen menyerupai payload SQLi."
            return None

    Kalau tidak ada handler terdaftar untuk sebuah kategori, semua tool call
    di kategori itu otomatis LOLOS tanpa validasi tambahan (opt-in per
    kategori -- kategori yang belum didaftarkan guardrail-nya tetap jalan
    normal seperti sebelumnya, tidak mengubah perilaku existing).

    PENTING: registry ini cuma menyimpan & memanggil fungsi validasi. Node
    LangGraph yang benar-benar mengeksekusi tool untuk kategori "pentest"
    (atau kategori lain yang mau divalidasi) harus memanggil
    `GuardrailRegistry.check(kategori, nama_tool, args)` untuk SETIAP
    tool_call SEBELUM menjalankan tool-nya, dan kalau hasilnya bukan None,
    kirim itu sebagai ToolMessage lalu SKIP eksekusi tool yang sesungguhnya.
    """
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