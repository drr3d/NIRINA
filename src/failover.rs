use std::time::{Duration, Instant};

use axum::{
    body::Bytes,
    http::{HeaderValue, StatusCode, header},
};
use serde_json::Value;

use crate::{config::Upstream, proxy::AppState};

/// Respons yang diteruskan ke klien: sukses, atau galat klien (4xx) yang bukan urusan failover.
pub struct Balasan {
    pub status: StatusCode,
    pub content_type: Option<HeaderValue>,
    pub body: Bytes,
    /// Nama upstream yang menjawab.
    pub upstream: String,
}

/// Kegagalan yang layak dicoba ke upstream lain.
pub enum Gagal {
    Timeout,
    Sambung,
    Terputus,
    /// Respons melebihi `server.max_response_mb`: upstream dianggap bermasalah (tanpa retry di upstream yang sama).
    TerlaluBesar,
    /// 2xx tetapi isinya bukan objek JSON (mis. halaman portal dari `base_url` yang salah): bukan jawaban chat yang sah.
    ResponsRusak,
    /// 3xx: gateway tidak mengikuti redirect (akan mengulang body request ke alamat yang ditunjuk upstream).
    Redirect,
    Http {
        status: StatusCode,
        content_type: Option<HeaderValue>,
        body: Bytes,
        retry_after: Option<u64>,
    },
}

pub struct Akhir {
    /// Jumlah panggilan ke upstream (termasuk retry).
    pub percobaan: u32,
    pub hasil: Result<Balasan, Gagal>,
}

impl Gagal {
    /// Retry di upstream yang sama hanya untuk galat sementara. 429/401/403, respons rusak dan redirect langsung pindah upstream.
    fn boleh_diulang(&self) -> bool {
        match self {
            Gagal::Timeout | Gagal::Sambung | Gagal::Terputus => true,
            Gagal::TerlaluBesar | Gagal::ResponsRusak | Gagal::Redirect => false,
            Gagal::Http { status, .. } => status.is_server_error() || *status == StatusCode::REQUEST_TIMEOUT,
        }
    }

    fn ringkas(&self) -> String {
        match self {
            Gagal::Timeout => "timeout".into(),
            Gagal::Sambung => "koneksi gagal".into(),
            Gagal::Terputus => "respons terputus".into(),
            Gagal::TerlaluBesar => "respons terlalu besar".into(),
            Gagal::ResponsRusak => "respons 2xx bukan JSON".into(),
            Gagal::Redirect => "upstream mengalihkan (3xx)".into(),
            Gagal::Http { status, .. } => format!("HTTP {}", status.as_u16()),
        }
    }
}

/// Status HTTP upstream yang berarti "upstream bermasalah" (bukan "request klien salah").
/// 401/403 masuk sini karena berarti key provider di gateway salah/dicabut, dan upstream lain mungkin sehat.
fn layak_failover(s: StatusCode) -> bool {
    s.is_server_error() || matches!(s.as_u16(), 401 | 403 | 408 | 429)
}

/// Jawaban chat completions yang sah selalu objek JSON. Pemeriksaan murah (karakter pertama); validitas penuh diperiksa
/// saat respons diparse di `proxy`.
fn tampak_objek_json(body: &[u8]) -> bool {
    body.iter().find(|b| !b.is_ascii_whitespace()) == Some(&b'{')
}

/// Mengirim request ke upstream alias dengan urutan sehat-dulu, retry untuk galat sementara, dan failover.
/// `req` harus objek JSON; field `model` ditimpa per upstream.
pub async fn jalankan(s: &AppState, alias: &str, upstreams: &[&Upstream], req: &mut Value, nama_key: &str) -> Akhir {
    let mulai = Instant::now();
    let rt = s.runtime();
    let cfg = &rt.config;
    let ids: Vec<&str> = upstreams.iter().map(|u| u.id.as_str()).collect();
    let mut percobaan = 0u32;
    let mut terakhir: Option<Gagal> = None;

    for (idx, dingin) in s.kesehatan.urutan(&ids, mulai) {
        let up = upstreams[idx];
        let jatah = if dingin { 1 } else { 1 + cfg.max_retries };
        // Kegagalan di upstream INI (bukan sisa dari upstream sebelumnya) dan apakah budget waktu habis di tengah jalan.
        let (mut gagal_di_sini, mut habis) = (false, false);

        for ke in 0..jatah {
            if ke > 0 {
                let jeda = cfg.retry_backoff.saturating_mul(1 << (ke - 1).min(6));
                tokio::time::sleep(jeda.min(cfg.total_timeout.saturating_sub(mulai.elapsed()))).await;
            }
            let Some(sisa) = cfg.total_timeout.checked_sub(mulai.elapsed()).filter(|d| !d.is_zero()) else {
                habis = true;
                break;
            };
            percobaan += 1;
            match kirim_sekali(s, up, req, up.timeout.min(sisa), cfg.max_response_bytes, alias, nama_key).await {
                Ok(b) => {
                    s.kesehatan.sukses(&up.id);
                    return Akhir { percobaan, hasil: Ok(b) };
                }
                Err(g) => {
                    let ulang = g.boleh_diulang();
                    terakhir = Some(g);
                    gagal_di_sini = true;
                    if !ulang {
                        break;
                    }
                }
            }
        }

        // Dicatat walau budget waktu habis di tengah retry: upstream yang macet harus turun prioritas, bukan terus
        // membakar seluruh budget setiap request.
        if gagal_di_sini && let Some(g) = &terakhir {
            let cooldown = match g {
                Gagal::Http { status, retry_after: Some(d), .. } if *status == StatusCode::TOO_MANY_REQUESTS => {
                    Duration::from_secs((*d).clamp(1, 300))
                }
                _ => cfg.cooldown,
            };
            s.kesehatan.gagal(&up.id, cooldown, Instant::now());
            tracing::warn!(alias, key = nama_key, upstream = %up.name, sebab = %g.ringkas(), cooldown_dtk = cooldown.as_secs(), "upstream gagal, pindah ke berikutnya bila ada");
        }
        if habis {
            break;
        }
    }

    Akhir { percobaan, hasil: Err(terakhir.unwrap_or(Gagal::Timeout)) }
}

async fn kirim_sekali(
    s: &AppState,
    up: &Upstream,
    req: &mut Value,
    timeout: Duration,
    batas_respons: usize,
    alias: &str,
    nama_key: &str,
) -> Result<Balasan, Gagal> {
    if let Some(o) = req.as_object_mut() {
        o.insert("model".into(), Value::String(up.model.clone()));
    }
    let mut rb = s.client.post(&up.url).timeout(timeout).json(&*req);
    if let Some(k) = &up.api_key {
        rb = rb.bearer_auth(k);
    }
    let resp = rb.send().await.map_err(|e| {
        let timeout = e.is_timeout();
        tracing::debug!(alias, key = nama_key, upstream = %up.name, timeout, "kirim gagal: {}", e.without_url());
        if timeout { Gagal::Timeout } else { Gagal::Sambung }
    })?;

    let status = resp.status();
    if status.is_redirection() {
        tracing::warn!(alias, key = nama_key, upstream = %up.name, status = status.as_u16(), "upstream mengalihkan request; periksa base_url");
        return Err(Gagal::Redirect);
    }
    let content_type = resp.headers().get(header::CONTENT_TYPE).cloned();
    let retry_after = resp.headers().get(header::RETRY_AFTER).and_then(|v| v.to_str().ok()).and_then(|v| v.trim().parse::<u64>().ok());
    let body = match baca_terbatas(resp, batas_respons).await {
        Ok(b) => b,
        Err(g) => {
            tracing::warn!(alias, key = nama_key, upstream = %up.name, sebab = %g.ringkas(), "gagal membaca respons upstream");
            return Err(g);
        }
    };

    if status.is_success() {
        if !tampak_objek_json(&body) {
            tracing::warn!(alias, key = nama_key, upstream = %up.name, "respons 2xx upstream bukan objek JSON");
            return Err(Gagal::ResponsRusak);
        }
        Ok(Balasan { status, content_type, body, upstream: up.name.clone() })
    } else if !layak_failover(status) {
        Ok(Balasan { status, content_type, body, upstream: up.name.clone() })
    } else {
        Err(Gagal::Http { status, content_type, body, retry_after })
    }
}

/// Membaca body respons dengan batas ukuran, supaya upstream yang rusak/jahat tidak bisa menghabiskan memori gateway.
async fn baca_terbatas(mut resp: reqwest::Response, batas: usize) -> Result<Bytes, Gagal> {
    let panjang = resp.content_length();
    if panjang.is_some_and(|n| n > batas as u64) {
        return Err(Gagal::TerlaluBesar);
    }
    let mut isi = Vec::with_capacity(panjang.map_or(4096, |n| n as usize).min(batas));
    // Timeout per-request reqwest juga mencakup pembacaan body: body yang macet adalah timeout, bukan koneksi terputus.
    while let Some(potongan) = resp.chunk().await.map_err(|e| if e.is_timeout() { Gagal::Timeout } else { Gagal::Terputus })? {
        if isi.len() + potongan.len() > batas {
            return Err(Gagal::TerlaluBesar);
        }
        isi.extend_from_slice(&potongan);
    }
    Ok(Bytes::from(isi))
}
