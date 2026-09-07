import time
import threading
import datetime
import json
from typing import Any, Dict, List

from flask import Flask, jsonify
from langchain_core.messages import HumanMessage

from database import automation_db as db
from core_agent.registry import ToolRegistry
from core_agent.agent_factory.agent_factory import muat_plugins, buat_llm

# --- [WAJIB] Trigger semua @ToolRegistry.register(...) di plugins/ sebelum
# daemon ini nyoba manggil tool apapun -- tanpa ini ToolRegistry kosong. ---
muat_plugins()

llm_extract = buat_llm(
    "automation_extract",
    model_default="qwen2.5:3b",
    provider_default="ollama",
    num_ctx_default=4096,
)

_GUARDRAIL_PROMPT_EKSTRAKSI = (
    "Kamu adalah asisten EKSTRAKSI TEKS murni. Tugasmu HANYA membaca teks input "
    "dan mengeluarkan SATU nilai hasil ekstraksi sesuai instruksi -- TIDAK LEBIH. "
    "JANGAN menjawab pertanyaan lain, JANGAN berbasa-basi, JANGAN menjelaskan "
    "alasanmu, JANGAN berpura-pura punya kemampuan memanggil tool/fungsi apapun "
    "(kamu memang TIDAK punya akses tool sama sekali). Keluarkan HANYA nilai "
    "hasil ekstraksi, tanpa tanda kutip, tanpa penjelasan tambahan, tanpa markdown."
)

POLLING_INTERVAL_DETIK = 30
DAEMON_PORT = 5055

app = Flask(__name__)

# Guard sederhana biar 1 automation gak dieksekusi 2x bersamaan (mis. polling
# loop & tombol "Jalankan Sekarang" kebetulan nyerempet di waktu yang sama).
_sedang_jalan_lock = threading.Lock()
_sedang_jalan: set = set()

_waktu_mulai_daemon = time.time()

# ==========================================
# CEK APA SATU AUTOMATION SUDAH WAKTUNYA JALAN
# ==========================================
def is_due(automation: Dict[str, Any]) -> bool:
    last_run = automation.get("last_run_at")
    sekarang = time.time()

    if automation["tipe_jadwal"] == "INTERVAL":
        interval_detik = int(automation["waktu_eksekusi"])
        if last_run is None:
            return True  # belum pernah jalan sama sekali -> langsung due
        return (sekarang - last_run) >= interval_detik

    elif automation["tipe_jadwal"] == "DAILY":
        jam_target = datetime.datetime.strptime(automation["waktu_eksekusi"], "%H:%M").time()
        sekarang_dt = datetime.datetime.now()
        if sekarang_dt.time() < jam_target:
            return False  # belum sampai jam-nya hari ini
        if last_run is None:
            return True
        last_run_dt = datetime.datetime.fromtimestamp(last_run)
        return last_run_dt.date() < sekarang_dt.date()  # sudah lewat jam TAPI belum pernah jalan HARI INI

    elif automation["tipe_jadwal"] == "EVENT":

        dep_id = automation.get("depends_on_id")
        target_status = automation.get("depends_on_status")
        if not dep_id or not target_status:
            return False  # data cacat/belum lengkap -- jangan jalanin apa-apa

        riwayat_dep = db.ambil_riwayat(dep_id, limit=1)
        if not riwayat_dep:
            return False  # dependensi belum pernah jalan sama sekali

        run_terbaru = riwayat_dep[0]
        if last_run is not None and run_terbaru["mulai_at"] <= last_run:
            return False  # run dependensi ini SUDAH pernah direspons sebelumnya

        if target_status != "ANY" and run_terbaru["status"] != target_status:
            return False  # dependensi barusan jalan, tapi statusnya gak cocok

        return True

    return False

# ==========================================
# RESOLVE 1 ARGUMEN (manual ATAU dari output step lain)
# ==========================================
def _resolve_args(step: Dict[str, Any], hasil_per_step: Dict[int, str]) -> Dict[str, Any]:
    resolved = {}
    for nama_param, spek in (step.get("args") or {}).items():
        if spek.get("type") == "from_step":
            idx = spek["step"]
            resolved[nama_param] = hasil_per_step.get(idx)
        else:
            resolved[nama_param] = spek.get("value")
    return resolved

# ==========================================
# EKSEKUSI 1 STEP "AI TRANSFORM"
# ==========================================
def _eksekusi_ai_transform(step: Dict[str, Any], hasil_per_step: Dict[int, str]) -> str:
    """Ekstrak 1 nilai dari output step sebelumnya pakai llm_extract.
    3 bagian prompt digabung: guardrail default (TETAP, gak bisa dihapus
    user) + konteks tool tujuan (opsional, auto dari docstring+skema kalau
    `target_tool_untuk_konteks` diisi) + instruksi custom user (opsional)."""
    idx = step.get("sumber_step")
    teks_sumber = hasil_per_step.get(idx, "") if idx is not None else ""

    bagian_prompt = [_GUARDRAIL_PROMPT_EKSTRAKSI]

    target_tool = step.get("target_tool_untuk_konteks")
    target_param = step.get("target_param_untuk_konteks")
    if target_tool:
        tool_obj = next((t for t in ToolRegistry.get_tools("safe") if t.name == target_tool), None)
        if tool_obj:
            skema = tool_obj.args or {}

            if target_param and target_param in skema:
                info_param = skema[target_param]
                bagian_prompt.append(
                    f"Konteks: hasil ekstraksimu akan dipakai sebagai NILAI untuk SATU "
                    f"argumen '{target_param}' (tipe: {info_param.get('type', 'string')}) "
                    f"dari tool '{tool_obj.name}'.\nDeskripsi tool: {tool_obj.description}\n"
                    f"PENTING: kamu HANYA perlu menghasilkan nilai untuk argumen "
                    f"'{target_param}' ini -- JANGAN pedulikan argumen lain yang mungkin "
                    f"dibutuhkan tool ini, itu diisi terpisah."
                )
            else:
                bagian_prompt.append(
                    "Konteks: hasil ekstraksimu akan dipakai LANGSUNG sebagai argumen "
                    f"untuk tool berikut -- sesuaikan formatnya:\nNama tool: {tool_obj.name}\n"
                    f"Deskripsi: {tool_obj.description}\n"
                    f"Parameter yang dibutuhkan: {json.dumps(skema, ensure_ascii=False)}"
                )

    instruksi_custom = step.get("instruksi")
    if instruksi_custom:
        bagian_prompt.append(f"Instruksi tambahan dari user: {instruksi_custom}")

    bagian_prompt.append(f"TEKS INPUT:\n{teks_sumber}")
    bagian_prompt.append("HASIL EKSTRAKSI (langsung nilainya saja, tanpa embel-embel):")

    prompt_final = "\n\n".join(bagian_prompt)
    respons = llm_extract.invoke([HumanMessage(content=prompt_final)])
    return str(respons.content).strip()

# ==========================================
# EKSEKUSI 1 AUTOMATION (semua step-nya, berurutan)
# ==========================================
def eksekusi_automation(automation: Dict[str, Any]):
    automation_id = automation["id"]
    mulai_at = time.time()
    hasil_per_step: Dict[int, str] = {}
    detail_log: List[Dict[str, Any]] = []
    status_akhir = "SUKSES"

    try:
        steps = json.loads(automation["steps_json"])

        print(f"\n[⚡ DAEMON] ▶️ MULAI '{automation['nama_alur']}' ({len(steps)} step)")

        if steps and isinstance(steps[0], str):
            # Format LAMA (dari sebelum refactor) -- gak ada argumen tersimpan,
            # gak bisa dieksekusi presisi. Skip, catat sebagai gagal supaya
            # kelihatan di riwayat kenapa automation ini gak pernah jalan.
            raise RuntimeError(
                "Automation ini masih format lama (tanpa argumen tersimpan) -- "
                "buat ulang lewat UI supaya argumen tool ikut tersimpan."
            )

        i = 0  # index manual (bukan for-loop) -- perlu bisa "dilompatin" buat percabangan
        while i < len(steps):
            step = steps[i]
            tipe_step = step.get("type", "tool")  # default "tool" -> backward compat step lama

            if tipe_step == "ai_transform":
                print(f"[⚡ DAEMON] '{automation['nama_alur']}' step {i+1}/{len(steps)}: AI Transform (dari step {step.get('sumber_step', '?')+1 if step.get('sumber_step') is not None else '?'})...")
                hasil_str = _eksekusi_ai_transform(step, hasil_per_step)
                print(f"[⚡ DAEMON]   -> hasil ekstraksi: {hasil_str[:120]!r}")
                detail_log.append({"step": i + 1, "type": "ai_transform", "sumber_step": step.get("sumber_step"), "hasil": hasil_str[:500]})
            else:
                nama_tool = step["tool"]
                tool_obj = next((t for t in ToolRegistry.get_tools("safe") if t.name == nama_tool), None)
                if tool_obj is None:
                    raise RuntimeError(f"Step {i+1}: tool '{nama_tool}' tidak ditemukan di registry (mungkin plugin-nya dinonaktifkan).")

                args_resolved = _resolve_args(step, hasil_per_step)
                print(f"[⚡ DAEMON] '{automation['nama_alur']}' step {i+1}/{len(steps)}: memanggil {nama_tool}({args_resolved})...")

                hasil = tool_obj.invoke(args_resolved)
                hasil_str = str(hasil)

                print(f"[⚡ DAEMON]   -> hasil {nama_tool}: {hasil_str[:300]!r}")
                detail_log.append({"step": i + 1, "type": "tool", "tool": nama_tool, "args": args_resolved, "hasil": hasil_str[:500]})

            hasil_per_step[i] = hasil_str

            stop_if = step.get("stop_if_output_contains")
            if stop_if and stop_if in hasil_str:
                print(f"[⚡ DAEMON] Step {i+1} hasil mengandung '{stop_if}' -> hentikan alur (goal sudah tercapai).")
                status_akhir = "DIHENTIKAN"
                break

            lompat = step.get("lompat_jika")
            if lompat and lompat.get("mengandung") and lompat["mengandung"] in hasil_str:
                tujuan = lompat.get("ke_step")
                if isinstance(tujuan, int) and tujuan > i:
                    print(f"[⚡ DAEMON] Step {i+1} hasil mengandung '{lompat['mengandung']}' -> loncat ke step {tujuan+1}.")
                    i = tujuan
                    continue
                else:
                    print(f"[⚡ DAEMON] ⚠️ Target lompatan step {i+1} tidak valid (ke_step={tujuan}) -- diabaikan, lanjut normal.")

            i += 1

    except Exception as e:
        status_akhir = "GAGAL"
        detail_log.append({"error": f"{type(e).__name__}: {e}"})
        print(f"[⚡ DAEMON] ❌ '{automation['nama_alur']}' GAGAL: {e}")

    selesai_at = time.time()
    db.catat_run(automation_id, mulai_at, selesai_at, status_akhir, detail_log)
    db.update_last_run(automation_id, selesai_at)
    print(f"[⚡ DAEMON] '{automation['nama_alur']}' selesai -> {status_akhir} ({selesai_at - mulai_at:.1f}s)")

def _eksekusi_dengan_guard(automation: Dict[str, Any]):
    """Wrapper -- cegah 1 automation dieksekusi 2x bersamaan."""
    aid = automation["id"]
    with _sedang_jalan_lock:
        if aid in _sedang_jalan:
            print(f"[⚡ DAEMON] Skip '{automation['nama_alur']}' -- masih ada eksekusi lain berjalan.")
            return
        _sedang_jalan.add(aid)
    try:
        eksekusi_automation(automation)
    finally:
        with _sedang_jalan_lock:
            _sedang_jalan.discard(aid)

# ==========================================
# LOOP POLLING (jalan di background thread)
# ==========================================
def _loop_polling():
    while True:
        try:
            for automation in db.ambil_automation_running():
                if is_due(automation):
                    threading.Thread(target=_eksekusi_dengan_guard, args=(automation,), daemon=True).start()
        except Exception as e:
            print(f"[⚡ DAEMON] ⚠️ Error di loop polling (dilewati, coba lagi cycle berikutnya): {e}")
        time.sleep(POLLING_INTERVAL_DETIK)

# ==========================================
# FLASK ROUTES
# ==========================================
@app.route("/status", methods=["GET"])
def status():
    aktif = db.ambil_automation_running()
    return jsonify({
        "status": "ok",
        "uptime_detik": round(time.time() - _waktu_mulai_daemon, 1),
        "automation_aktif": len(aktif),
        "sedang_eksekusi": list(_sedang_jalan),
    })

@app.route("/run_now/<automation_id>", methods=["POST"])
def run_now(automation_id):
    automation = db.ambil_satu_automation(automation_id)
    if automation is None:
        return jsonify({"error": "Automation tidak ditemukan"}), 404
    threading.Thread(target=_eksekusi_dengan_guard, args=(automation,), daemon=True).start()
    return jsonify({"status": "triggered", "nama_alur": automation["nama_alur"]})

if __name__ == "__main__":
    db.init_db()
    print(f"[⚡ DAEMON] Mulai polling tiap {POLLING_INTERVAL_DETIK} detik, Flask di port {DAEMON_PORT}...")
    threading.Thread(target=_loop_polling, daemon=True).start()
    app.run(host="127.0.0.1", port=DAEMON_PORT)