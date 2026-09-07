import importlib, os
import pkgutil
import json
from typing import Callable, Dict

from ..config import config_path, app_dir
from ..registry import ToolRegistry
from .voyager.skill_lib import SkillLibrary

# --- [DYNAMIC CONFIG] MEMBACA SETTING MODEL ---
model_chat_name = "qwen3.5-9b-q4_k_m:latest" 
top_k_tools_agent = 8
maks_umur_skill_gagal_jam = 24
min_similarity_skill_sukses = 0.80
min_similarity_skill_gagal = 0.65
aktifkan_gorilla_tool_rag = True

default_provider = "ollama"

if config_path.exists():
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)

            model_chat_name = config_data.get("model_chat_agent", model_chat_name)
            top_k_tools_agent = int(config_data.get("top_k_tools_agent", top_k_tools_agent))
            maks_umur_skill_gagal_jam = float(config_data.get("maks_umur_skill_gagal_jam", maks_umur_skill_gagal_jam))
            min_similarity_skill_sukses = float(config_data.get("min_similarity_skill_sukses", min_similarity_skill_sukses))
            min_similarity_skill_gagal = float(config_data.get("min_similarity_skill_gagal", min_similarity_skill_gagal))
            aktifkan_gorilla_tool_rag = bool(config_data.get("aktifkan_gorilla_tool_rag", aktifkan_gorilla_tool_rag))
            
            if config_data.get("use_gateway") is True:
                default_provider = "openai"
            else:
                default_provider = config_data.get("default_provider", default_provider).lower()
    except Exception:
        pass

maks_umur_skill_gagal_detik = (
    maks_umur_skill_gagal_jam * 3600 if maks_umur_skill_gagal_jam > 0 else None
)

# ==========================================
# [FRAMEWORK CORE] DYNAMIC TOKEN ROUTER (CHAIN, N-LLM)
# ==========================================
class DynamicTokenRouterLLM:
    """
    Proxy Wrapper untuk LangChain BaseChatModel -- versi CHAIN, bukan cuma
    primary/fallback. Nerima list LLM terurut, tiap entry punya threshold
    SENDIRI (bukan 1 threshold global) -- jadi bisa punya lebih dari 2 LLM,
    masing-masing kapasitasnya beda.

    DUA lapis proteksi (bukan cuma 1):
      1. PRE-EMPTIF: estimasi token (pakai tiktoken kalau ada, fallback ke
         heuristik char/3.5 kalau tiktoken belum terinstall) dicek ke
         threshold tiap entry SEBELUM manggil -- entry yang thresholdnya
         kelewatan di-skip duluan, gak usah dicoba.
      2. REAKTIF: entry yang LOLOS estimasi tapi TETAP gagal pas beneran
         di-invoke (mis. API rate-limit/token-limit error dari provider,
         karena estimasi kita cuma perkiraan -- overhead system prompt/tool
         schema/special token gak selalu match persis sama tokenizer asli
         provider) otomatis JATUH ke entry berikutnya di chain, bukan
         langsung raise ke atas.

    Format tiap entry di `llm_chain` (list of dict):
        {"llm": <BaseChatModel>, "threshold": int|None, "nama": str}
    `threshold=None` berarti "selalu diterima, apapun estimasi token-nya"
    -- pas buat entry PALING TERAKHIR di chain (mis. model lokal yang gak
    ada limit TPM keras), supaya selalu ada tempat jatuh paling akhir.
    """
    def __init__(self, llm_chain: list, default_threshold: int = 6500, default_minthreshold: int = 0, encoding_name: str = "cl100k_base"):
        if not llm_chain:
            raise ValueError("llm_chain tidak boleh kosong -- minimal 1 entry.")
        self.llm_chain = llm_chain
        self.default_threshold = default_threshold
        self.default_minthreshold = default_minthreshold
        self.encoding_name = encoding_name
        # Token skema tool (nama+deskripsi+parameter dari SEMUA tool yang
        # di-bind_tools()) -- ini komponen BESAR yang kelewat kalau cuma ngitung
        # .content pesan. Diisi sekali di bind_tools(), 0 sebelum tools di-bind.
        self._tool_schema_tokens = 0
        try:
            import tiktoken
            self._enc = tiktoken.get_encoding(encoding_name)
        except Exception as e:
            print(f"[🔀 ROUTER] ⚠️ tiktoken tidak tersedia ({type(e).__name__}) -- fallback ke heuristik char/3.5.")
            self._enc = None

    def _encode_len(self, text: str) -> int:
        """Helper 1 pintu buat ngitung token dari SATU string -- dipakai baik
        buat isi pesan maupun buat skema tool, biar konsisten metodenya."""
        if self._enc is not None:
            try:
                return len(self._enc.encode(text))
            except Exception:
                pass
        return int(len(text) / 3.5)

    def _hitung_token(self, messages) -> int:
        text_content = "\n".join(str(getattr(m, "content", "") or m) for m in messages) if isinstance(messages, list) else str(messages)
        return self._encode_len(text_content) + self._tool_schema_tokens

    def _hitung_tool_schema_tokens(self, tools) -> int:
        try:
            from langchain_core.utils.function_calling import convert_to_openai_tool
            skema = [convert_to_openai_tool(t) for t in tools]
            schema_text = json.dumps(skema, default=str, ensure_ascii=False)
        except Exception as e:
            print(f"[🔀 ROUTER] ⚠️ Gagal serialize skema tool buat estimasi ({type(e).__name__}), pakai perkiraan kasar dari repr().")
            schema_text = str(tools)
        return self._encode_len(schema_text)

    def bind_tools(self, tools, **kwargs):
        new_chain = [
            {**entry, "llm": entry["llm"].bind_tools(tools, **kwargs)}
            for entry in self.llm_chain
        ]
        new_instance = DynamicTokenRouterLLM(new_chain, self.default_threshold, self.encoding_name)
        new_instance._tool_schema_tokens = self._hitung_tool_schema_tokens(tools)
        print(f"[🔀 ROUTER] Skema {len(tools)} tool ≈ {new_instance._tool_schema_tokens} token (dihitung sekali, dipakai tiap estimasi berikutnya).")
        return new_instance

    def invoke(self, messages, **kwargs):
        estimasi_prompt = self._hitung_token(messages)
        error_terakhir = None

        for i, entry in enumerate(self.llm_chain):
            nama = entry.get("nama", f"llm_{i}")
            threshold = entry.get("threshold", self.default_threshold)
            # butuh ini min_threshold, biar bisa menghemat pemanggilan groq, 
            #  karena token kecil lebih baik dilempar ke ollama local.
            #  kecuali punya akun berbayar dengan token unlimit, maka abaikan aja min_threshold
            min_threshold = entry.get("min_threshold", self.default_minthreshold)
   
            reserved = getattr(entry["llm"], "max_tokens", None) or 0
            estimasi_efektif = estimasi_prompt + reserved

            if threshold is not None and estimasi_efektif > threshold:
                print(f"[🔀 ROUTER] Skip '{nama}' (prompt {estimasi_prompt} + reserved {reserved} = {estimasi_efektif} > threshold {threshold})")
                continue

            print(estimasi_efektif, min_threshold, type(estimasi_efektif), type(min_threshold))
            if min_threshold is not None and estimasi_efektif < int(min_threshold):
                print(f"[🔀 ROUTER] Skip '{nama}' To Save (prompt {estimasi_prompt} + reserved {reserved} = {estimasi_efektif} < threshold {threshold})")
                continue

            try:
                label_threshold = "tanpa batas" if threshold is None else f"threshold {threshold}"
                print(f"\n[🔀 ROUTER] Coba '{nama}' (prompt {estimasi_prompt} + reserved {reserved} = {estimasi_efektif} token, {label_threshold})...")
                return entry["llm"].invoke(messages, **kwargs)
            except Exception as e:
                print(f"[🔀 ROUTER] ⚠️ '{nama}' GAGAL saat invoke ({type(e).__name__}: {e}) -> lanjut ke entry berikutnya di chain.")
                error_terakhir = e
                continue

        raise RuntimeError(
            f"[🔀 ROUTER] Semua {len(self.llm_chain)} LLM di chain gagal/di-skip untuk estimasi {estimasi_efektif} token."
        ) from error_terakhir

# ==========================================
# ==========================================
# [FRAMEWORK CORE] LLM PROVIDER REGISTRY
# ==========================================
class LLMProviderRegistry:
    _builders: Dict[str, Callable] = {}

    @classmethod
    def register(cls, provider_name: str, builder_func: Callable):
        """
        Aturan Integrasi (Contract):
        builder_func harus menerima argumen: (model_name: str, temperature: float, **kwargs)
        dan WAJIB mengembalikan instance dari subclass langchain_core.language_models.BaseChatModel
        """
        cls._builders[provider_name.lower()] = builder_func

    @classmethod
    def get_builder(cls, provider_name: str) -> Callable:
        builder = cls._builders.get(provider_name.lower())
        if not builder:
            raise ValueError(
                f"Provider '{provider_name}' belum terdaftar di LLMProviderRegistry. "
                f"Provider yang tersedia: {list(cls._builders.keys())}"
            )
        return builder

# --- IMPLEMENTASI DEFAULT PROVIDERS ---
def _build_ollama(model_name: str, temperature: float, **kwargs):
    from langchain_ollama import ChatOllama
    # Hapus parameter standar yang tidak dipakai Ollama
    kwargs.pop("api_key", None)
    base_url = kwargs.pop("base_url", None)
    
    # Jika base_url diisi di config, teruskan ke Ollama. Jika tidak, abaikan.
    if base_url:
        kwargs["base_url"] = base_url
        
    return ChatOllama(model=model_name, temperature=temperature, **kwargs)

def _build_openai(model_name: str, temperature: float, **kwargs):
    from langchain_openai import ChatOpenAI
    
    raw_api_key = kwargs.pop("api_key", None)
    api_key = raw_api_key if raw_api_key else "sk-dummy" # Gateway lokal butuh string terisi
    
    base_url = kwargs.pop("base_url", None)
    
    kwargs.pop("num_ctx", None)
    kwargs.pop("reasoning", None)
    
    openai_args = {
        "model": model_name, 
        "api_key": api_key, 
        "temperature": temperature
    }
    if base_url:
        openai_args["base_url"] = base_url
        
    return ChatOpenAI(**openai_args, **kwargs)

def _build_groq(model_name: str, temperature: float, **kwargs):
    from langchain_groq import ChatGroq
    raw_api_key = kwargs.pop("api_key", "")

    api_key = raw_api_key or os.environ.get("GROQ_API_KEY")

    kwargs.pop("base_url", None) # Groq pakai endpoint paten
    kwargs.pop("num_ctx", None)
    kwargs.pop("reasoning", None)
    
    # Passing api_key hanya jika nilainya valid (bukan string kosong)
    groq_args = {
        "model": model_name,
        "temperature": temperature,
    }
    if api_key:
        groq_args["api_key"] = api_key
        
    return ChatGroq(**groq_args, **kwargs)

def _build_cerebras(model_name: str, temperature: float, **kwargs):
    from langchain_cerebras import ChatCerebras

    raw_api_key = kwargs.pop("api_key", "")
    api_key = raw_api_key or os.environ.get("CEREBRAS_API_KEY")  # kosong -> ChatCerebras fallback ke env CEREBRAS_API_KEY

    kwargs.pop("base_url", None)  # Cerebras pakai endpoint paten (cloud.cerebras.ai)
    kwargs.pop("num_ctx", None)   # bukan parameter yang dikenal Cerebras

    reasoning_flag = kwargs.pop("reasoning", None)
    reasoning_effort = kwargs.pop("reasoning_effort", None)
    if reasoning_effort is None and reasoning_flag is not None:
        reasoning_effort = "medium" if reasoning_flag else "low"

    cerebras_args = {
        "model": model_name,
        "temperature": temperature,
    }
    if api_key:
        cerebras_args["api_key"] = api_key
    if reasoning_effort and "gpt-oss" in model_name.lower():
        cerebras_args["reasoning_effort"] = reasoning_effort

    return ChatCerebras(**cerebras_args, **kwargs)

# Daftarkan keempat provider bawaan framework ke dalam Registry
LLMProviderRegistry.register("ollama", _build_ollama)
LLMProviderRegistry.register("openai", _build_openai)
LLMProviderRegistry.register("groq", _build_groq)
LLMProviderRegistry.register("cerebras", _build_cerebras)

# ==========================================
# [FRAMEWORK CORE] FACTORY LLM DINAMIS
# ==========================================
def buat_llm(
    peran: str,
    model_default: str,
    temperature_default: float = 0.3,
    num_ctx_default: int = 8192,
    reasoning_default: bool = False,
    provider_default: str = None,
    **extra_kwargs,
):
    """
    Factory yang mendelegasikan pembuatan LLM ke LLMProviderRegistry.
    Tidak ada if-elif provider di sini. Sepenuhnya modular!
    """
    cfg_peran = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg_peran = (json.load(f).get("llm_roles", {}) or {}).get(peran, {}) or {}
        except Exception:
            pass

    provider = cfg_peran.get("provider", provider_default or default_provider).lower()
    model_final = cfg_peran.get("model", cfg_peran.get("gateway_model", model_default))

    # Kumpulkan semua konfigurasi mentah
    builder_kwargs = {
        "temperature": cfg_peran.get("temperature", temperature_default),
        "api_key": cfg_peran.get("api_key", ""),
        "base_url": cfg_peran.get("base_url", ""),
        "num_ctx": cfg_peran.get("num_ctx", num_ctx_default),
        "reasoning": cfg_peran.get("reasoning", reasoning_default),
    }

    # Backward compatibility untuk config lama use_gateway: true
    if provider == "openai" and not builder_kwargs["base_url"]:
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                if json.load(f).get("use_gateway") is True:
                    builder_kwargs["base_url"] = "http://localhost:4000/v1"

    # Gabungkan dengan extra_kwargs dari pemanggil
    builder_kwargs.update(extra_kwargs)

    # 🚀 MAGIC HAPPENS HERE: Ambil builder dari Registry dan jalankan
    builder_func = LLMProviderRegistry.get_builder(provider)
    llm = builder_func(model_name=model_final, **builder_kwargs)

    print(f"[BUAT_LLM] Peran '{peran}' -> provider='{provider}' model='{model_final}'")
    return llm

# ==========================================
# [FRAMEWORK CORE] FACTORY SKILL LIBRARY DINAMIS
# ==========================================
def buat_skill_library(
    peran: str,
    persist_dir_default: str = "./skill_library_db",
    collection_name_default: str = "agent_skills",
    embedding_backend_default: str = "ollama",
    ollama_model_default: str = "nomic-embed-text",
    st_model_name_default: str = "all-MiniLM-L6-v2",
    **extra_kwargs,
):
    """
    [FRAMEWORK CORE] Factory generik buat bikin SATU instance SkillLibrary
    (Voyager-style, ChromaDB-backed) untuk SATU "peran"/domain bebas apa saja
    -- prinsipnya sama dengan buat_llm(): nambah skill library baru (mis.
    buat agent domain lain yang butuh koleksi/persist_dir terpisah) = panggil
    fungsi ini dari file aplikasimu, BUKAN edit file ini.

    Override lewat config.json:

        {
          "skill_library_roles": {
            "<peran>": {
              "persist_dir": "...",
              "collection_name": "...",
              "embedding_backend": "ollama" | "st" | "custom",
              "ollama_model": "...",
              "st_model_name": "..."
            }
          }
        }

    Kalau "<peran>" tidak ada di config.json, semua "..._default" dipakai
    apa adanya.

    Args:
        peran: nama bebas (dipakai buat lookup config.json & buat log).
        persist_dir_default, collection_name_default, embedding_backend_default,
        ollama_model_default, st_model_name_default: fallback kalau config.json
            tidak override peran ini.
        **extra_kwargs: diteruskan APA ADANYA ke constructor SkillLibrary
            (mis. ollama_base_url, custom_embedding_fn) tanpa perlu ubah
            signature fungsi ini lagi.

    Return: instance SkillLibrary, siap dipakai.
    """
    cfg_peran = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg_peran = (json.load(f).get("skill_library_roles", {}) or {}).get(peran, {}) or {}
        except Exception:
            cfg_peran = {}

    persist_dir = cfg_peran.get("persist_dir", persist_dir_default)
    collection_name = cfg_peran.get("collection_name", collection_name_default)
    embedding_backend = cfg_peran.get("embedding_backend", embedding_backend_default)

    sl = SkillLibrary(
        persist_dir=persist_dir,
        collection_name=collection_name,
        embedding_backend=embedding_backend,
        ollama_model=cfg_peran.get("ollama_model", ollama_model_default),
        st_model_name=cfg_peran.get("st_model_name", st_model_name_default),
        **extra_kwargs,
    )
    print(
        f"[BUAT_SKILL_LIBRARY] Peran '{peran}' -> collection='{collection_name}' "
        f"persist_dir='{persist_dir}' backend='{embedding_backend}'"
    )
    return sl

# ==========================================
# [FRAMEWORK CORE] PLUGIN AUTO-DISCOVERY (DIPANGGIL EKSPLISIT, BUKAN OTOMATIS)
# ==========================================
def muat_plugins(folder=None, nama_package_import: str = "plugins"):
    target_folder = folder or (app_dir / "plugins")
    if target_folder.exists():
        for _, module_name, _ in pkgutil.iter_modules([str(target_folder)]):
            importlib.import_module(f"{nama_package_import}.{module_name}")

# ==========================================
# [FRAMEWORK CORE] BOOTSTRAP TOOL -- URUTAN WAJIB, DIGABUNG JADI SATU PANGGILAN
# ==========================================
def factory_tools_init(
    *nama_tools_wajib: str,
    folder_plugins=None,
    nama_package_import: str = "plugins",
    llm_mentah = None
):
    print(
        "\n[⚙️ Bootstrap] Memuat plugin & menyinkronkan Tool-RAG -- pastikan "
        "LLM/skill_library project ini SUDAH dibuat sebelum baris ini "
        "dieksekusi (lihat docstring inisialisasi_plugin_dan_tools)."
    )
    muat_plugins(folder_plugins, nama_package_import)
    ToolRegistry.sync_tools_to_db(llm = llm_mentah)
    if nama_tools_wajib:
        ToolRegistry.daftar_tool_wajib(*nama_tools_wajib)