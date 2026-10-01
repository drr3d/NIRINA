use std::time::{Duration, Instant};

use nigate::limiter::{Limiter, Tolak};

fn dtk(n: u64) -> Duration {
    Duration::from_secs(n)
}

#[test]
fn tanpa_batas_selalu_lolos() {
    let l = Limiter::default();
    let t0 = Instant::now();
    for _ in 0..1000 {
        assert!(l.coba(1, None, None, 999_999, t0).is_ok());
    }
}

#[test]
fn rpm_habis_lalu_terisi_merata() {
    let l = Limiter::default();
    let t0 = Instant::now();
    assert!(l.coba(1, Some(2), None, 1, t0).is_ok());
    assert!(l.coba(1, Some(2), None, 1, t0).is_ok());
    let Err(Tolak::Rpm { tunggu }) = l.coba(1, Some(2), None, 1, t0) else { panic!("harus ditolak RPM") };
    assert_eq!(tunggu, dtk(30), "2 rpm = 1 token tiap 30 detik");

    assert!(l.coba(1, Some(2), None, 1, t0 + dtk(29)).is_err());
    assert!(l.coba(1, Some(2), None, 1, t0 + dtk(30)).is_ok());
    assert!(l.coba(1, Some(2), None, 1, t0 + dtk(30)).is_err(), "baru satu token terisi");
}

#[test]
fn bucket_tidak_melebihi_kapasitas() {
    let l = Limiter::default();
    let t0 = Instant::now();
    assert!(l.coba(1, Some(2), None, 1, t0).is_ok());
    // diam 1 jam: tetap maksimal 2 token, bukan 120
    let nanti = t0 + dtk(3600);
    assert!(l.coba(1, Some(2), None, 1, nanti).is_ok());
    assert!(l.coba(1, Some(2), None, 1, nanti).is_ok());
    assert!(l.coba(1, Some(2), None, 1, nanti).is_err());
}

#[test]
fn tpm_memakai_estimasi_dan_menolak_saat_saldo_kurang() {
    let l = Limiter::default();
    let t0 = Instant::now();
    assert!(l.coba(1, None, Some(1000), 400, t0).is_ok());
    assert!(l.coba(1, None, Some(1000), 400, t0).is_ok()); // sisa 200
    let Err(Tolak::Tpm { tunggu }) = l.coba(1, None, Some(1000), 400, t0) else { panic!("harus ditolak TPM") };
    assert_eq!(tunggu, dtk(12), "butuh 200 token lagi, laju 1000/60 per detik");
    assert!(l.coba(1, None, Some(1000), 400, t0 + dtk(12)).is_ok());
}

#[test]
fn koreksi_pengembalian_dan_pemakaian_lebih() {
    let l = Limiter::default();
    let t0 = Instant::now();
    assert!(l.coba(1, None, Some(1000), 900, t0).is_ok());
    l.koreksi_token(1, -800); // pemakaian asli hanya 100
    assert!(l.coba(1, None, Some(1000), 800, t0).is_ok(), "saldo pulih setelah koreksi");

    let l = Limiter::default();
    assert!(l.coba(2, None, Some(1000), 100, t0).is_ok());
    l.koreksi_token(2, 4900); // ternyata memakai 5000 token -> saldo negatif
    let Err(Tolak::Tpm { tunggu }) = l.coba(2, None, Some(1000), 100, t0) else { panic!("harus ditolak") };
    assert!(tunggu > dtk(200), "utang besar harus ditunggu lama, dapat {tunggu:?}");
}

#[test]
fn request_lebih_besar_dari_kapasitas_lolos_saat_penuh_lalu_menjadi_utang() {
    let l = Limiter::default();
    let t0 = Instant::now();
    assert!(l.coba(1, None, Some(1000), 5000, t0).is_ok());
    assert!(l.coba(1, None, Some(1000), 1, t0).is_err());
}

#[test]
fn penolakan_salah_satu_batas_tidak_memakai_jatah_yang_lain() {
    let l = Limiter::default();
    let t0 = Instant::now();
    assert!(l.coba(1, Some(1), Some(100), 100, t0).is_ok()); // rpm habis, tpm habis
    // RPM & TPM sama-sama kosong; tunggu TPM pulih tapi RPM belum (1 rpm = 60 dtk)
    assert!(l.coba(1, Some(1), Some(100), 100, t0 + dtk(30)).is_err());
    // ditolak tidak boleh menguras apa pun: pada detik 60 keduanya penuh lagi
    assert!(l.coba(1, Some(1), Some(100), 100, t0 + dtk(60)).is_ok());
}

#[test]
fn key_berbeda_terpisah_dan_batas_bisa_dicabut() {
    let l = Limiter::default();
    let t0 = Instant::now();
    assert!(l.coba(1, Some(1), None, 1, t0).is_ok());
    assert!(l.coba(1, Some(1), None, 1, t0).is_err());
    assert!(l.coba(2, Some(1), None, 1, t0).is_ok(), "key lain tidak terpengaruh");
    assert!(l.coba(1, None, None, 1, t0).is_ok(), "batas dicabut -> bebas");
}

#[test]
fn retry_after_dibulatkan_ke_atas_minimal_satu() {
    assert_eq!(Tolak::Rpm { tunggu: Duration::from_millis(100) }.retry_after_detik(), 1);
    assert_eq!(Tolak::Tpm { tunggu: Duration::from_millis(30_100) }.retry_after_detik(), 31);
}
