from typing import Dict, Any, Callable
import json

# ==========================================
# 1. UI REGISTRY (Agar Kontributor Bisa Menambah Custom View Tool)
# ==========================================
class ToolFormatterRegistry:
    """Registry untuk memformat tampilan Tool di UI secara dinamis (Plugin System)."""
    _registry: Dict[str, Callable[[Dict[str, Any]], str]] = {}

    @classmethod
    def register(cls, tool_name: str):
        """Decorator untuk mendaftarkan parser tampilan tool baru."""
        def decorator(func: Callable[[Dict[str, Any]], str]):
            cls._registry[tool_name] = func
            return func
        return decorator

    @classmethod
    def format(cls, tool_name: str, args: Dict[str, Any]) -> str:
        """Format argumen tool berdasarkan formatter yang terdaftar."""
        if tool_name in cls._registry:
            return cls._registry[tool_name](args)
        # Default fallback formatter
        return f"   Argumen: `{json.dumps(args, ensure_ascii=False)}`"


# --- Contoh Kontributor Mendaftarkan Formatter khusus Lowongan Bulk ---
@ToolFormatterRegistry.register("posting_lowongan_bulk")
def format_bulk_lowongan(args: Dict[str, Any]) -> str:
    data_lowongan = args.get("daftar_lowongan", [])
    sub_detail = []
    for idx, lw in enumerate(data_lowongan, 1):
        sub_detail.append(
            f"  {idx}. **{lw.get('posisi')}**\n"
            f"     • Periode: {lw.get('tanggal_mulai')} s/d {lw.get('tanggal_selesai')}\n"
            f"     • Keyword: {lw.get('keyword_wajib')}"
        )
    return f"Menyimpan {len(data_lowongan)} Lowongan Sekaligus:\n" + "\n".join(sub_detail)

# ==========================================
# UI ADAPTER (Penterjemah State Mentah Graf -> Kebutuhan Frontend)
# ==========================================
class StreamlitAgentAdapter:
    """Adapter untuk menerjemahkan State Graf ke format UI Streamlit."""
    
    @staticmethod
    def process_state_to_ui(state) -> Dict[str, Any]:
        # PERUBAHAN DI SINI: Deteksi status butuh persetujuan secara universal
        if state.next:
            pesan_terakhir = state.values["messages"][-1]
            tool_calls = getattr(pesan_terakhir, "tool_calls", [])
            
            if tool_calls:
                detail_pesan = []
                for idx, tc in enumerate(tool_calls, 1):
                    nama_tool = tc["name"]
                    argumen_tool = tc["args"]
                    
                    formatted_arg = ToolFormatterRegistry.format(nama_tool, argumen_tool)
                    detail_pesan.append(f"{idx}. Tool: **{nama_tool}**\n{formatted_arg}")
                
                # Menampilkan nama Node yang sedang ditahan (opsional untuk info debug UI)
                node_tertahan = ", ".join(state.next)
                
                pesan_gabungan = (
                    f"### ⚠️ KONFIRMASI TINDAKAN (Menunggu di: {node_tertahan})\n"
                    "AI memerlukan konfirmasi persetujuan Anda untuk melakukan tindakan berikut:\n\n" + 
                    "\n\n".join(detail_pesan)
                )
                return {
                    "status": "butuh_persetujuan",
                    "tool": tool_calls[0]["name"] if len(tool_calls) == 1 else "multiple_tools",
                    "args": tool_calls[0]["args"] if len(tool_calls) == 1 else tool_calls,
                    "pesan": pesan_gabungan
                }

        # Skenario 2: Ekstraksi Link Download
        download_info = None
        if "messages" in state.values:
            for msg in reversed(state.values["messages"]):
                if getattr(msg, "name", None) == "ambil_tautan_download_cv":
                    try:
                        res_data = json.loads(msg.content)
                        if res_data.get("status") == "tersedia":
                            download_info = {
                                "nama_file": res_data["nama_file"],
                                "path": res_data["path"]
                            }
                        break
                    except Exception:
                        pass

        # Skenario 3: Ambil Jawaban Akhir
        jawaban_final = "Maaf, tidak ada respons yang valid dari agen."
        if "messages" in state.values:
            messages = state.values["messages"]
            
            last_human_idx = -1
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].type == "human":
                    last_human_idx = i
                    break
            
            if last_human_idx != -1:
                kumpulan_teks_ai = []
                teks_cadangan_pemikiran = [] # [BARU] Menyimpan pemikiran AI sebelum blank
                
                for msg in messages[last_human_idx + 1:]:
                    if msg.type == "ai" and msg.content and msg.content.strip():
                        if not getattr(msg, "tool_calls", []):
                            # Teks murni (Kesimpulan akhir)
                            kumpulan_teks_ai.append(msg.content.strip())
                        else:
                            # Teks pengantar sebelum memanggil tool
                            teks_cadangan_pemikiran.append(msg.content.strip())
                
                # [BARU] Logika Evaluasi UI
                if kumpulan_teks_ai:
                    jawaban_final = "\n\n---\n\n".join(kumpulan_teks_ai)
                elif teks_cadangan_pemikiran:
                    # Jika AI blank di akhir, selamatkan UI dengan menampilkan pemikirannya
                    jawaban_final = (
                        "*(AI sedang menyusun data)*\n\n" + 
                        "\n\n".join(teks_cadangan_pemikiran) + 
                        "\n\n---\n⚠️ *Sistem: AI telah berhasil menarik data dari alat, namun tidak dapat merangkumnya. Silakan persempit kriteria pencarian Anda.*"
                    )

        return {
            "status": "selesai",
            "pesan": jawaban_final,
            "download_info": download_info
        }