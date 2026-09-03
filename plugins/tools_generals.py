import os,requests

from core_agent.registry import ToolRegistry

def cari_di_internet(query: str, engine: str = "tavily") -> str:
    """
    GUNAKAN tools ini untuk mencari informasi melalui internet.
    
    Parameter:
    - query: Kata kunci pencarian.
    - engine: Mesin pencari. Pilihan: 'tavily' (terbaik untuk AI), 'google_api', atau 'ddg' (tanpa API key). Default: 'tavily'.
    """
    if engine == "duckduckgo":
        engine = "ddg"

    hasil_pencarian = []
    
    # ---------------------------------------------------------
    # 1. ENGINE: TAVILY (The Best for LLM)
    # ---------------------------------------------------------
    if engine == "tavily":
        tavily_key = os.environ.get("TAVILY_API_KEY") # Ambil dari environment variable
        if not tavily_key:
            return "Error: TAVILY_API_KEY tidak ditemukan di environment. Coba gunakan engine='ddg'."
            
        try:
            url = "https://api.tavily.com/search"
            payload = {
                "api_key": tavily_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 3,
                "include_answer": False
            }
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                data = res.json()
                for r in data.get("results", []):
                    hasil_pencarian.append({
                        "engine": "Tavily",
                        "title": r.get("title", ""),
                        "body": r.get("content", ""), # Tavily memberikan content yang sangat bersih
                        "href": r.get("url", "")
                    })
            else:
                return f"Error Tavily API: {res.status_code} - {res.text}. Coba engine='ddg'."
        except Exception as e:
            return f"Gagal mengeksekusi Tavily: {e}. Coba engine='ddg'."

    # ---------------------------------------------------------
    # 2. ENGINE: GOOGLE CUSTOM SEARCH API (Official)
    # ---------------------------------------------------------
    elif engine == "google_api":
        google_key = os.environ.get("GOOGLE_SEARCHAPI_KEY") # https://console.cloud.google.com/apis/
        cx = os.environ.get("GOOGLE_CX_ID") # Search Engine ID https://programmablesearchengine.google.com/
        
        if not google_key or not cx:
            return "Error: GOOGLE_API_KEY atau GOOGLE_CX_ID tidak ditemukan. Coba gunakan engine='ddg'."
            
        try:
            url = "https://www.googleapis.com/customsearch/v1"
            params = {
                "key": google_key,
                "cx": cx,
                "q": query,
                "num": 7
            }
            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 200:
                data = res.json()
                for r in data.get("items", []):
                    hasil_pencarian.append({
                        "engine": "Google API",
                        "title": r.get("title", ""),
                        "body": r.get("snippet", ""),
                        "href": r.get("link", "")
                    })
            else:
                return f"Error Google API: {res.status_code}. Coba engine='ddg'."
        except Exception as e:
            return f"Gagal mengeksekusi Google API: {e}. Coba engine='ddg'."

    # ---------------------------------------------------------
    # 3. ENGINE: DUCKDUCKGO (Fallback - No API Key)
    # ---------------------------------------------------------
    elif engine == "ddg":
        try:
            from ddgs import DDGS
            with DDGS() as mesin:
                hasil_ddg = list(mesin.text(query, max_results=3))
                for r in hasil_ddg:
                    hasil_pencarian.append({
                        "engine": "DuckDuckGo",
                        "title": r.get("title", ""),
                        "body": r.get("body", ""),
                        "href": r.get("href", "")
                    })
        except Exception as e:
            return f"Gagal mencari di DuckDuckGo: {e}. Semua engine gagal."
            
    else:
        return f"Engine '{engine}' tidak dikenali. Pilih 'tavily', 'google_api', atau 'ddg'."

    # ---------------------------------------------------------
    # FORMATTING OUTPUT
    # ---------------------------------------------------------
    if not hasil_pencarian:
        return "Tidak ada hasil ditemukan."

    ringkasan = []
    for i, r in enumerate(hasil_pencarian, 1):
        judul = r.get("title", "")
        isi = (r.get("body", "") or "")[:400]
        sumber = r.get("href", "")
        sumber_engine = r.get("engine", "")
        
        ringkasan.append(f"{i}. [{sumber_engine}] {judul}\n   {isi}\n   Sumber: {sumber}")

    return "\n\n".join(ringkasan)