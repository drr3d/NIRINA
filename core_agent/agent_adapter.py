from typing import Dict, Any, Callable
import json

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
        # Skenario 1: Deteksi status butuh persetujuan secara universal
        if state.next:
            tool_calls = []
            invalid_calls = []
            
            # Cari backwards untuk menemukan tool_calls ATAU invalid_tool_calls
            for msg in reversed(state.values["messages"]):
                calls = getattr(msg, "tool_calls", [])
                invalids = getattr(msg, "invalid_tool_calls", [])
                
                if calls or invalids:
                    tool_calls = calls
                    invalid_calls = invalids
                    break # Langsung berhenti saat menemukan percobaan tool terbaru
            
            # Jika ditemukan Tool valid ataupun cacat (invalid)
            if tool_calls or invalid_calls:
                detail_pesan = []
                counter = 1
                
                # 1. Tangani Tool yang Valid
                for tc in tool_calls:
                    nama_tool = tc["name"]
                    argumen_tool = tc["args"]
                    formatted_arg = ToolFormatterRegistry.format(nama_tool, argumen_tool)
                    detail_pesan.append(f"{counter}. Tool: **{nama_tool}**\n{formatted_arg}")
                    counter += 1
                
                # 2. Tangani Tool yang Cacat/Invalid (Penyakit Local LLM)
                for tc in invalid_calls:
                    nama_tool = tc.get("name", "UnknownTool")
                    err_msg = tc.get("error", "Kesalahan parsing JSON dari AI")
                    detail_pesan.append(f"{counter}. ❌ Tool: **{nama_tool}** (GAGAL PARSING)\n   Alasan: `{err_msg}`\n   Raw Data: `{tc.get('raw', '')}`")
                    counter += 1
                
                node_tertahan = ", ".join(state.next)
                
                pesan_gabungan = (
                    f"### ⚠️ KONFIRMASI TINDAKAN (Menunggu di: {node_tertahan})\n"
                    "AI memerlukan konfirmasi Anda untuk tindakan berikut:\n\n" + 
                    "\n\n".join(detail_pesan)
                )
                
                return {
                    "status": "butuh_persetujuan",
                    "tool": tool_calls[0]["name"] if tool_calls else "invalid_tool",
                    "args": tool_calls[0]["args"] if tool_calls else {},
                    "pesan": pesan_gabungan
                }
            else:
                # 3. DETEKSI SABOTASE: Graph tertahan, tapi Tool tidak ada!
                return {
                    "status": "butuh_persetujuan", 
                    "tool": "error_state_cleaner",
                    "args": {},
                    "pesan": (
                        f"### ⚠️ GRAPH TERTUNDA DI NODE: {', '.join(state.next)}\n\n"
                        "**DIAGNOSTIK SISTEM:** Adapter tidak menemukan riwayat pemanggilan Tool. "
                        "Kemungkinan besar pesan Tool Call baru saja **terhapus oleh [🧹 State Cleaner]** "
                        "sebelum UI sempat membacanya."
                    )
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
        #print(f"process_state_to_ui state.values: {state.values}")
        if "messages" in state.values:
            from langchain_core.messages import AIMessage
            
            messages = state.values["messages"]
            kumpulan_kesimpulan = []
            print(f"process_state_to_ui state.values: {messages[-2:]}")
            # 1. Cari input Human Asli terakhir (ABAIKAN pesan intervensi [SISTEM])
            last_human_idx = -1
            for i in range(len(messages) - 1, -1, -1):
                msg = messages[i]
                if getattr(msg, "type", "") == "human":
                    content_str = str(getattr(msg, "content", "") or "")
                    # Kunci perbaikan: abaikan pesan otomatis dari sistem/router
                    if not content_str.startswith("[SISTEM"):
                        last_human_idx = i
                        break
            
            start_idx = last_human_idx + 1 if last_human_idx != -1 else 0
            
            # 2. Filter pesan AI
            if start_idx < len(messages):
                for msg in messages[start_idx:]:
                    is_ai_msg = isinstance(msg, AIMessage) or getattr(msg, "type", "") == "ai"
                    
                    if is_ai_msg:
                        has_tools = bool(getattr(msg, "tool_calls", []))
                        
                        if not has_tools:
                            isi_teks = str(getattr(msg, "content", "") or "").strip()
                            
                            # Abaikan jika ini adalah pesan error failsafe TETAPI sudah ada jawaban AI yang valid sebelumnya
                            is_failsafe_msg = "kesulitan menyelesaikan pemeriksaan ini secara otomatis" in isi_teks
                            if is_failsafe_msg and len(kumpulan_kesimpulan) > 0:
                                continue

                            if isi_teks:
                                kumpulan_kesimpulan.append(isi_teks)
                
                # 3. Ambil jawaban terbaik/terakhir dari kesimpulan yang valid
                if kumpulan_kesimpulan:
                    jawaban_final = kumpulan_kesimpulan[-1]

        return {
            "status": "selesai",
            "pesan": jawaban_final,
            "download_info": download_info
        }