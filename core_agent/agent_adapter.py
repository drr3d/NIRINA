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
        print(f"process_state_to_ui state.values: {state.values}")
        if "messages" in state.values:
            from langchain_core.messages import AIMessage
            
            messages = state.values["messages"]
            kumpulan_kesimpulan = []
            
            # 1. Cari input Human terakhir
            last_human_idx = -1
            for i in range(len(messages) - 1, -1, -1):
                if getattr(messages[i], "type", "") == "human":
                    last_human_idx = i
                    break
            
            start_idx = last_human_idx + 1 if last_human_idx != -1 else 0
            print(f"process_state_to_ui last_human_idx: {last_human_idx}\nmessages: {messages}\nstart_idx|len messages: {start_idx} | {len(messages)}")
            # 2. Filter pesan AI
            if start_idx < len(messages):
                for msg in messages[start_idx:]:
                    is_ai_msg = isinstance(msg, AIMessage) or getattr(msg, "type", "") == "ai"
                    
                    if is_ai_msg:
                        # Cek apakah pesan ini memanggil tool?
                        has_tools = bool(getattr(msg, "tool_calls", []))
                        
                        # KITA HANYA PEDULI PADA PESAN YANG *TIDAK* MEMANGGIL TOOL
                        # Karena ini adalah pesan yang ditujukan langsung ke User
                        if not has_tools:
                            isi_teks = str(getattr(msg, "content", "") or "").strip()
                            kwargs = getattr(msg, "additional_kwargs", {})
                            #teks_pemikiran = str(kwargs.get("reasoning_content", "") or "").strip()
                            
                            blok_teks = []
                            #if teks_pemikiran:
                            #    blok_teks.append(f"> 🧠 **Pemikiran AI:**\n> {teks_pemikiran}")
                            if isi_teks:
                                blok_teks.append(isi_teks)
                                
                            teks_gabungan = "\n\n".join(blok_teks)
                            if teks_gabungan:
                                kumpulan_kesimpulan.append(teks_gabungan)
                                #kumpulan_kesimpulan.append("")
                
                # 3. Logika UI Terakhir
                if kumpulan_kesimpulan:
                    # AMBIL HANYA KESIMPULAN YANG PALING AKHIR [-1]
                    jawaban_final = kumpulan_kesimpulan[-1]

        return {
            "status": "selesai",
            "pesan": jawaban_final,
            "download_info": download_info
        }