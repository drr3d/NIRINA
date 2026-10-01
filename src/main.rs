use std::{sync::Arc, time::Duration};

use anyhow::{Context, Result, bail};
use nigate::{
    AppState,
    admin::admin_app,
    app,
    config::Config,
    guardrail::{Guardrail, Laporan},
    keys::KeyStore,
    stats::{Baris, Kelompok, Statistik, ringkasan_dari_file, sekarang_ms},
};
use tracing_subscriber::EnvFilter;

const BANTUAN: &str = "\
Pemakaian:
  nigate [--config <file>] [serve]          jalankan gateway (bawaan)
  nigate [--config <file>] key create <nama>   buat virtual key (key asli hanya tampil sekali)
  nigate [--config <file>] key list            daftar key
  nigate [--config <file>] healthcheck         cek /healthz lokal (exit 0 = sehat)
  nigate admin token                          buat token acak untuk API admin
  nigate [--config <file>] guardrail cek [file]   uji aturan guardrail pada teks (stdin/file)
  nigate [--config <file>] stats [--jam N | --hari N] [--per semua|key|alias|upstream|hari]   ringkasan pemakaian
  nigate [--config <file>] key limit <nama> [--rpm N|none] [--tpm N|none]   atur batas per menit
  nigate [--config <file>] key revoke <nama>   cabut key
  nigate [--config <file>] key enable <nama>   aktifkan lagi key yang dicabut

Config: --config/-c, atau env NIGATE_CONFIG, atau ./nigate.toml.";

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .init();

    let (path, perintah) = baca_argumen();
    let perintah: Vec<&str> = perintah.iter().map(String::as_str).collect();
    if matches!(perintah.as_slice(), ["help"] | ["--help"] | ["-h"]) {
        println!("{BANTUAN}");
        return Ok(());
    }
    // Tidak butuh config, jadi bisa dijalankan sebelum config ada.
    if matches!(perintah.as_slice(), ["admin", "token"]) {
        let mut acak = [0u8; 32];
        getrandom::fill(&mut acak).map_err(|e| anyhow::anyhow!("gagal mengambil bilangan acak: {e}"))?;
        let token: String = acak.iter().map(|b| format!("{b:02x}")).collect();
        println!("Token admin baru (simpan; set sebagai env NIGATE_ADMIN_TOKEN untuk gateway dan untuk UI):\n\n  {token}\n");
        return Ok(());
    }
    let cfg = Config::from_file(&path)?;

    match perintah.as_slice() {
        [] | ["serve"] => serve(cfg, &path).await,
        ["key", aksi, rest @ ..] => perintah_key(&cfg, aksi, rest),
        ["stats", flags @ ..] => perintah_stats(&cfg, flags),
        ["healthcheck"] => healthcheck(&cfg).await,
        ["guardrail", "cek", rest @ ..] => perintah_guardrail_cek(&cfg, rest),
        _ => bail!("perintah tidak dikenal.\n\n{BANTUAN}"),
    }
}

fn baca_argumen() -> (String, Vec<String>) {
    let mut path = std::env::var("NIGATE_CONFIG").ok();
    let mut sisa = Vec::new();
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        if a == "--config" || a == "-c" {
            path = it.next();
        } else {
            sisa.push(a);
        }
    }
    (path.unwrap_or_else(|| "nigate.toml".into()), sisa)
}

fn perintah_key(cfg: &Config, aksi: &str, rest: &[&str]) -> Result<()> {
    let store = KeyStore::open(&cfg.db_path)?;
    match (aksi, rest) {
        ("create", [nama]) => {
            let (info, token) = store.create(nama)?;
            println!("Key '{}' dibuat. Simpan sekarang, key ini tidak akan ditampilkan lagi:\n\n  {token}\n", info.name);
        }
        ("list", []) => {
            if store.list()?.is_empty() {
                println!("(belum ada key)");
            }
            for k in store.list()? {
                println!(
                    "{:>3}  {:<24} {}…  {:<8} rpm={} tpm={}",
                    k.id,
                    k.name,
                    k.prefix,
                    if k.active { "aktif" } else { "DICABUT" },
                    tampil_batas(k.rpm),
                    tampil_batas(k.tpm)
                );
            }
        }
        ("limit", [nama, flags @ ..]) if !flags.is_empty() => {
            let Some(k) = store.list()?.into_iter().find(|k| k.name == *nama) else { bail!("key '{nama}' tidak ditemukan") };
            let (mut rpm, mut tpm) = (k.rpm, k.tpm);
            for pasangan in flags.chunks(2) {
                let [flag, nilai] = pasangan else { bail!("setiap flag butuh nilai (angka atau 'none')") };
                let v = if *nilai == "none" {
                    None
                } else {
                    Some(nilai.parse::<u64>().map_err(|_| anyhow::anyhow!("nilai '{nilai}' bukan angka"))?)
                };
                match *flag {
                    "--rpm" => rpm = v,
                    "--tpm" => tpm = v,
                    lain => bail!("flag '{lain}' tidak dikenal (pakai --rpm / --tpm)"),
                }
            }
            store.set_limits(nama, rpm, tpm)?;
            println!("Key '{nama}': rpm={} tpm={}", tampil_batas(rpm), tampil_batas(tpm));
        }
        ("revoke", [nama]) | ("enable", [nama]) => {
            let aktif = aksi == "enable";
            if !store.set_active(nama, aktif)? {
                bail!("key '{nama}' tidak ditemukan");
            }
            println!("Key '{nama}' {}.", if aktif { "diaktifkan" } else { "dicabut" });
        }
        _ => bail!("perintah key tidak dikenal.\n\n{BANTUAN}"),
    }
    Ok(())
}

fn perintah_stats(cfg: &Config, flags: &[&str]) -> Result<()> {
    let (mut jam, mut kelompok) = (24u64, Kelompok::Semua);
    for pasangan in flags.chunks(2) {
        let [flag, nilai] = pasangan else { bail!("setiap flag butuh nilai") };
        match *flag {
            "--jam" => jam = nilai.parse().map_err(|_| anyhow::anyhow!("--jam harus angka"))?,
            "--hari" => jam = nilai.parse::<u64>().map_err(|_| anyhow::anyhow!("--hari harus angka"))? * 24,
            "--per" => kelompok = Kelompok::dari_teks(nilai)?,
            lain => bail!("flag '{lain}' tidak dikenal (pakai --jam, --hari, --per)"),
        }
    }
    let sekarang = sekarang_ms();
    let dari = sekarang - jam as i64 * 3_600_000;
    let baris = ringkasan_dari_file(&cfg.stats_db_path, dari, sekarang + 1, kelompok)?;
    println!("Periode: {jam} jam terakhir");
    if baris.is_empty() {
        println!("(tidak ada request tercatat)");
        return Ok(());
    }
    println!(
        "{:<22} {:>7} {:>6} {:>6} {:>6} {:>6} {:>8} {:>6} {:>7} {:>10} {:>10} {:>9} {:>9}",
        "kelompok",
        "request",
        "ok",
        "klien",
        "limit",
        "guard",
        "upstream",
        "gw",
        "temuan",
        "tok_masuk",
        "tok_keluar",
        "rata(ms)",
        "maks(ms)"
    );
    for Baris {
        kelompok,
        request,
        ok,
        klien,
        limit,
        guardrail,
        upstream,
        gateway,
        temuan,
        token_masuk,
        token_keluar,
        latensi_rata_ms,
        latensi_maks_ms,
    } in baris
    {
        println!(
            "{kelompok:<22} {request:>7} {ok:>6} {klien:>6} {limit:>6} {guardrail:>6} {upstream:>8} {gateway:>6} {temuan:>7} {token_masuk:>10} {token_keluar:>10} {latensi_rata_ms:>9.0} {latensi_maks_ms:>9}"
        );
    }
    Ok(())
}

/// Menguji aturan guardrail terhadap teks dari stdin (atau file): tampilkan jumlah temuan per aturan dan hasil redaksi.
/// Berguna untuk menyetel aturan/ambang entropi tanpa menjalankan gateway.
fn perintah_guardrail_cek(cfg: &Config, rest: &[&str]) -> Result<()> {
    use std::io::Read;
    let mut teks = String::new();
    match rest {
        [] => {
            std::io::stdin().read_to_string(&mut teks)?;
        }
        [file] => teks = std::fs::read_to_string(file)?,
        _ => bail!("pakai: nigate guardrail cek [file]   (tanpa file = baca stdin)"),
    }
    let g = Guardrail::baru(&cfg.guardrail)?;
    let mut lap = Laporan::default();
    let hasil = g.periksa(&teks, &mut lap);
    if lap.temuan.is_empty() {
        println!("Tidak ada temuan.");
        return Ok(());
    }
    println!("Temuan:");
    for (nama, n) in &lap.temuan {
        println!("  {nama}: {n}");
    }
    if !lap.diblok.is_empty() {
        println!("Akan DITOLAK (mode block): {}", lap.diblok.join(", "));
    }
    println!(
        "
--- teks setelah guardrail ---
{hasil}"
    );
    Ok(())
}

/// Pemeriksaan kesehatan tanpa curl: binary memeriksa dirinya sendiri lewat loopback (exit 0 = sehat).
async fn healthcheck(cfg: &Config) -> Result<()> {
    let port = cfg.listen.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()).context("server.listen tidak memuat port yang valid")?;
    let url = format!("http://127.0.0.1:{port}/healthz");
    let resp = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()?
        .get(&url)
        .send()
        .await
        .with_context(|| format!("gagal menghubungi {url}"))?;
    if !resp.status().is_success() {
        bail!("{url} menjawab {}", resp.status());
    }
    Ok(())
}

fn tampil_batas(v: Option<u64>) -> String {
    v.map_or("-".into(), |x| x.to_string())
}

async fn serve(cfg: Config, path: &str) -> Result<()> {
    let listen = cfg.listen.clone();
    let keys = Arc::new(KeyStore::open(&cfg.db_path)?);
    keys.pantau_perubahan(Duration::from_secs(2));

    tracing::info!(config = %path, model = cfg.models.len(), db = %cfg.db_path, "nigate mulai");
    if !cfg.auth_required {
        tracing::warn!("auth.required = false: /v1/* TERBUKA tanpa API key. Hanya untuk pengembangan lokal.");
    } else if keys.list()?.iter().all(|k| !k.active) {
        tracing::warn!("belum ada key aktif; semua request akan ditolak. Buat dengan: nigate key create <nama>");
    }
    for (alias, m) in &cfg.models {
        for u in &m.upstreams {
            if let (Some(env), None) = (&u.key_env, &u.api_key) {
                tracing::warn!("model '{alias}': env {env} kosong, request ke model ini akan dijawab 503");
            }
        }
    }

    let listener =
        tokio::net::TcpListener::bind(&listen).await.with_context(|| format!("gagal mendengarkan di {listen} (port sudah dipakai?)"))?;
    tracing::info!("mendengarkan di {listen}");
    let statistik = Arc::new(if cfg.stats_enabled {
        Statistik::buka(&cfg.stats_db_path, cfg.stats_retention_days)?
    } else {
        tracing::warn!("stats.enabled = false: statistik pemakaian tidak dicatat");
        Statistik::nonaktif()
    });

    // API admin di listener terpisah; hanya dijalankan bila ada token (tanpa token = tidak ada pintu admin sama sekali).
    let admin_listener = if !cfg.admin_enabled {
        None
    } else if cfg.admin_token.is_none() {
        tracing::warn!("API admin TIDAK dijalankan: env token admin kosong. Buat token: nigate admin token, lalu set NIGATE_ADMIN_TOKEN.");
        None
    } else {
        let l = tokio::net::TcpListener::bind(&cfg.admin_listen)
            .await
            .with_context(|| format!("gagal mendengarkan API admin di {} (port sudah dipakai?)", cfg.admin_listen))?;
        if !l.local_addr()?.ip().is_loopback() {
            tracing::warn!(
                "API admin mendengarkan di {} (bukan loopback). Pastikan hanya bisa dijangkau dari jaringan tepercaya; jangan buka ke jaringan luar.",
                cfg.admin_listen
            );
        }
        tracing::info!("API admin di {}", cfg.admin_listen);
        Some(l)
    };

    let state = AppState::new(cfg, keys)?.dengan_statistik(statistik.clone()).dengan_config_path(path);

    let (henti_tx, henti_rx) = tokio::sync::watch::channel(false);
    tokio::spawn(async move {
        sinyal_berhenti().await;
        let _ = henti_tx.send(true);
    });
    let tunggu = |mut rx: tokio::sync::watch::Receiver<bool>| async move {
        let _ = rx.wait_for(|v| *v).await;
    };
    let data = async { axum::serve(listener, app(state.clone())).with_graceful_shutdown(tunggu(henti_rx.clone())).await };
    let admin = async {
        match admin_listener {
            Some(l) => axum::serve(l, admin_app(state.clone())).with_graceful_shutdown(tunggu(henti_rx.clone())).await,
            None => Ok(()),
        }
    };
    // Setelah sinyal berhenti, request yang sedang berjalan diberi waktu selesai; lewat dari itu dipaksa berhenti
    // (upstream yang menggantung tidak boleh menahan gateway tanpa batas). Statistik tetap ditulis tuntas.
    let tenggang = state.runtime().config.shutdown_grace;
    let jalan = async { tokio::join!(data, admin) };
    let batas = async {
        let mut rx = henti_rx.clone();
        let _ = rx.wait_for(|v| *v).await;
        tokio::time::sleep(tenggang).await;
    };
    tokio::select! {
        (hasil_data, hasil_admin) = jalan => {
            hasil_data?;
            hasil_admin?;
        }
        _ = batas => tracing::warn!("request yang berjalan tidak selesai dalam {} dtk setelah sinyal berhenti; dipaksa berhenti", tenggang.as_secs()),
    }
    statistik.tutup();
    tracing::info!("nigate berhenti");
    Ok(())
}

async fn sinyal_berhenti() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        if let Ok(mut s) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            s.recv().await;
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! { _ = ctrl_c => {}, _ = term => {} }
}
