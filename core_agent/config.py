# --- KONFIGURASI DATABASE & LLM ---
import json
from pathlib import Path
app_dir = Path(__file__).resolve().parent.parent

db_path = (app_dir / "APPDB/chroma_db").resolve()
json_path = app_dir / "kandidat_profil.json" # considered to be remove
config_path = app_dir / "config.json"
sqlite_db_path = app_dir / "APPDB/hr_database.db"
knowledge_dir = app_dir / "knowledge_docs"
temp_dir = app_dir / "temp_uploads"

# --- FUNGSI BACA CONFIG ---
analytics_config_path = app_dir / "analytics_config.json"

def get_analytics_config():
    """Membaca konfigurasi analitik tingkat lanjut."""
    default_config = {
        "query_limits": {"max_group_by_results": 10, "enable_fuzzy_matching": False},
        "data_integrity": {
            "exclude_nulls_in_aggregation": True,
            "handle_outliers": {"min_valid_age": 17, "max_valid_age": 60},
            "deduplicate_by": ""
        },
        "security": {"mask_pii_for_staff": True}
    }
    if analytics_config_path.exists():
        try:
            with open(analytics_config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default_config