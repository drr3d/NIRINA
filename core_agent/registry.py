from langchain_core.tools import tool
from collections import defaultdict

class ToolRegistry:
    """Registry framework dinamis dengan Backward Compatibility penuh."""
    _tools = defaultdict(list)

    @classmethod
    def register(cls, is_sensitive: bool = None, category: str = None):
        """
        Mendukung 2 cara pendaftaran:
        1. Cara Lama: @ToolRegistry.register(is_sensitive=True)
        2. Cara Baru: @ToolRegistry.register(category="hr_tools")
        """
        # --- LOGIKA HYBRID (BACKWARD COMPATIBILITY) ---
        if category is None:
            # Jika user pakai format lama (is_sensitive)
            if is_sensitive is True:
                target_category = "sensitive"
            else:
                target_category = "safe" # Default jika False atau kosong
        else:
            # Jika user pakai format baru (category="...")
            target_category = category

        def decorator(func):
            langchain_tool = tool(func)
            cls._tools[target_category].append(langchain_tool)
            return langchain_tool
        return decorator

    @classmethod
    def get_tools(cls, category: str):
        return cls._tools.get(category, [])

    @classmethod
    def get_all_tools(cls):
        """Menggabungkan semua kategori untuk Otak LLM."""
        all_tools = []
        for tool_list in cls._tools.values():
            all_tools.extend(tool_list)
        return all_tools