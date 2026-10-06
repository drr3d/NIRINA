//! Tes waktu terbatas untuk guardrail pada masukan jahat. Hanya bermakna di mode release (di debug regex ~20x lebih lambat),
//! jadi dilewati di `cargo test` biasa. Jalankan: `./dev.sh test --release --test guardrail_perf`.

use std::time::{Duration, Instant};

use nigate::guardrail::{Guardrail, GuardrailCfg, Laporan};

fn g() -> Guardrail {
    Guardrail::baru(&GuardrailCfg::default()).unwrap()
}

/// Karakter pseudo-acak deterministik.
fn acak(n: usize, benih: u64) -> String {
    let a: Vec<char> = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789".chars().collect();
    let mut x = benih.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
    (0..n)
        .map(|_| {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            a[((x >> 33) as usize) % a.len()]
        })
        .collect()
}

fn pindai(teks: &str) -> (Duration, u32) {
    let g = g();
    let mulai = Instant::now();
    let mut lap = Laporan::default();
    let _ = g.periksa(teks, &mut lap);
    (mulai.elapsed(), lap.total())
}

#[test]
#[cfg_attr(debug_assertions, ignore = "hanya bermakna di mode release")]
fn banyak_penanda_begin_tanpa_end_dipindai_linear() {
    // Pola lama kuadratik: 16 ribu unit (~1,3 MB) memakan ~17 detik CPU, dan 10 MB hampir 17 menit.
    let unit = format!("-----BEGIN RSA PRIVATE KEY-----\n{}\n", acak(52, 1));
    let teks = unit.repeat(16_000);
    let (dt, temuan) = pindai(&teks);
    assert!(dt < Duration::from_secs(2), "pemindaian {} MB memakan {dt:?}; harus linear", teks.len() as f64 / 1_048_576.0);
    assert!(temuan >= 1, "kunci yang terpotong tetap harus terdeteksi");
}

#[test]
#[cfg_attr(debug_assertions, ignore = "hanya bermakna di mode release")]
fn blok_kunci_privat_utuh_tetap_satu_temuan_dan_tuntas_diredaksi() {
    let g = g();
    let isi: String = (0..40).map(|i| format!("{}\n", acak(64, 100 + i))).collect();
    let teks = format!("awal\n-----BEGIN PRIVATE KEY-----\n{isi}-----END PRIVATE KEY-----\nakhir");
    let mut lap = Laporan::default();
    let hasil = g.periksa(&teks, &mut lap).into_owned();
    assert_eq!(hasil, "awal\n[REDACTED:private_key]\nakhir");
    assert_eq!(lap.total(), 1);
}

#[test]
#[cfg_attr(debug_assertions, ignore = "hanya bermakna di mode release")]
fn temuan_entropi_dan_spesifik_dalam_jumlah_besar_dipindai_linear() {
    // Pengecekan tumpang tindih lama O(entropi x spesifik): 10 MB dengan ~257 ribu temuan memakan ~12 detik.
    let unit = format!("ghp_{} {} ", acak(36, 7), acak(40, 9));
    let teks = unit.repeat(125_000); // ~10 MB
    let (dt, temuan) = pindai(&teks);
    assert!(
        dt < Duration::from_secs(3),
        "pemindaian {:.1} MB dengan {temuan} temuan memakan {dt:?}; harus linear",
        teks.len() as f64 / 1_048_576.0
    );
    assert!(temuan >= 125_000);
}
