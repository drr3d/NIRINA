import importlib
import pkgutil
import json
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from ..config import config_path, app_dir

from .voyager.skill_lib import SkillLibrary

# --- [DYNAMIC CONFIG] MEMBACA SETTING MODEL ---
model_chat_name = "qwen3.5-9b-q4_k_m:latest" 
top_k_tools_agent = 8
maks_umur_skill_gagal_jam = 24
min_similarity_skill_sukses = 0.80
min_similarity_skill_gagal = 0.65

aktifkan_gorilla_tool_rag = True
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
    except Exception:
        pass

maks_umur_skill_gagal_detik = (
    maks_umur_skill_gagal_jam * 3600 if maks_umur_skill_gagal_jam > 0 else None
)
# ==========================================
# [FRAMEWORK CORE] FACTORY LLM DINAMIS
# ==========================================
use_gateway = False
if config_path.exists():
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            use_gateway = bool(json.load(f).get("use_gateway", use_gateway))
    except Exception:
        pass


def buat_llm(
    peran: str,
    model_default: str,
    temperature_default: float = 0.3,
    num_ctx_default: int = 8192,
    reasoning_default: bool = False,
    **extra_kwargs,
):
    """
    [FRAMEWORK CORE] Factory generik buat bikin SATU instance LLM (ChatOllama
    kalau use_gateway=False, atau ChatOpenAI ke gateway kalau True) untuk
    SATU "peran" bebas apa saja ("main", "planner", "coder", "evaluator",
    atau nama apa pun yang kamu mau) -- ini yang bikin agent_factory.py layak
    disebut framework: nambah LLM baru = panggil fungsi ini dari file
    aplikasimu sendiri, BUKAN edit file ini.

    Semua parameter "..._default" di sini cuma dipakai kalau config.json
    TIDAK override peran ini. Override lewat config.json:

        {
          "llm_roles": {
            "<peran>": {
              "model": "...",
              "temperature": 0.3,
              "num_ctx": 8192,
              "reasoning": true,
              "gateway_model": "...",
              "base_url": "http://localhost:4000/v1",
              "api_key": "sk-..."
            }
          }
        }

    Kalau "<peran>" tidak ada sama sekali di config.json, SEMUA nilai
    "..._default" dipakai apa adanya -- jadi bikin LLM baru bisa langsung
    `buat_llm("evaluator", model_default="qwen3.5:4b")` tanpa nyentuh
    config.json ATAU file ini sama sekali kalau default-nya udah cukup.

    Args:
        peran: nama bebas (dipakai buat lookup config.json & buat log).
        model_default: nama model Ollama (dipakai juga sebagai fallback nama
            model gateway kalau "gateway_model" tidak di-set di config).
        temperature_default, num_ctx_default, reasoning_default: fallback
            kalau config.json tidak override peran ini.
        **extra_kwargs: diteruskan APA ADANYA ke constructor ChatOllama/
            ChatOpenAI -- buat parameter yang belum "resmi" difasilitasi
            factory ini (mis. num_predict, top_p, dst) tanpa perlu ubah
            signature fungsi ini lagi tiap ada kebutuhan baru.

    Return: instance ChatOllama atau ChatOpenAI, siap dipakai.
    """
    cfg_peran = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg_peran = (json.load(f).get("llm_roles", {}) or {}).get(peran, {}) or {}
        except Exception:
            cfg_peran = {}

    if use_gateway:
        model_final = cfg_peran.get("gateway_model", model_default)
        llm = ChatOpenAI(
            model=model_final,
            base_url=cfg_peran.get("base_url", "http://localhost:4000/v1"),
            api_key=cfg_peran.get("api_key", ""),
            temperature=cfg_peran.get("temperature", temperature_default),
            **extra_kwargs,
        )
    else:
        model_final = cfg_peran.get("model", model_default)
        llm = ChatOllama(
            model=model_final,
            temperature=cfg_peran.get("temperature", temperature_default),
            num_ctx=cfg_peran.get("num_ctx", num_ctx_default),
            reasoning=cfg_peran.get("reasoning", reasoning_default),
            **extra_kwargs,
        )

    print(f"[BUAT_LLM] Peran '{peran}' -> {'gateway' if use_gateway else 'ollama'} model='{model_final}'")
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
    """
    [FRAMEWORK CORE] Auto-discover & import semua modul Python di dalam SATU
    folder plugin -- men-trigger decorator @ToolRegistry.register(...) (dan
    @GuardrailRegistry.register(...), dst) di tiap file plugin, jadi tool-nya
    otomatis kedaftar begitu function ini dipanggil.

    Args:
        folder: path folder plugin (default: app_dir / "plugins").
        nama_package_import: prefix nama package Python yang dipakai buat
            `importlib.import_module()` (default "plugins", cocok kalau
            foldernya memang bisa diimpor sebagai top-level package bernama
            "plugins" -- sesuaikan kalau struktur project-mu beda).

    CATATAN PENTING buat penulis plugin yang tool-nya butuh REFERENSI ke
    skill_lib atau panggil_otak_llm milik SATU file aplikasi tertentu (mis.
    lupakan_skill_gagal, atur_gorilla_tool_rag): JANGAN
    `from core_agent.factory_security import panggil_otak_llm` di LEVEL MODUL
    (bakal gagal -- lihat alasan #2 di atas). Import MODULE-nya saja
    (`from core_agent import factory_security`), lalu akses
    `factory_security.panggil_otak_llm` DI DALAM BODY FUNGSI tool (bukan di
    level modul) -- Python baru resolve atribut itu saat tool BENERAN
    dipanggil user, di titik mana factory_security.py sudah pasti selesai
    dieksekusi penuh. Lihat plugin_atur_gorilla_tool_rag.py dan
    plugin_lupakan_skill_gagal.py sebagai contoh pola yang benar.
    """
    target_folder = folder or (app_dir / "plugins")
    if target_folder.exists():
        for _, module_name, _ in pkgutil.iter_modules([str(target_folder)]):
            importlib.import_module(f"{nama_package_import}.{module_name}")