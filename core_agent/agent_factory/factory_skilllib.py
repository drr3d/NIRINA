# File for storing and centralizing skill library
from .agent_factory import buat_skill_library

defaults_kill_lib = buat_skill_library(
    "default",
    persist_dir_default="./skill_library_db",
    collection_name_default="agent_skills",
    embedding_backend_default="ollama",
    ollama_model_default="nomic-embed-text",
)