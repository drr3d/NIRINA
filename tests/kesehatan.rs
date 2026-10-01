use std::time::{Duration, Instant};

use nigate::kesehatan::Kesehatan;

fn dtk(n: u64) -> Duration {
    Duration::from_secs(n)
}

const IDS: [&str; 3] = ["a", "b", "c"];

#[test]
fn semua_sehat_mengikuti_urutan_config() {
    let k = Kesehatan::default();
    assert_eq!(k.urutan(&IDS, Instant::now()), vec![(0, false), (1, false), (2, false)]);
}

#[test]
fn yang_gagal_dipindah_ke_belakang_dan_pulih_setelah_cooldown() {
    let k = Kesehatan::default();
    let t0 = Instant::now();
    k.gagal("a", dtk(30), t0);
    assert_eq!(k.urutan(&IDS, t0 + dtk(1)), vec![(1, false), (2, false), (0, true)]);
    assert_eq!(k.urutan(&IDS, t0 + dtk(31)), vec![(0, false), (1, false), (2, false)], "cooldown habis -> kembali ke urutan asli");
}

#[test]
fn beberapa_cooldown_diurutkan_dari_yang_paling_cepat_pulih() {
    let k = Kesehatan::default();
    let t0 = Instant::now();
    k.gagal("a", dtk(60), t0);
    k.gagal("b", dtk(10), t0);
    assert_eq!(k.urutan(&IDS, t0 + dtk(1)), vec![(2, false), (1, true), (0, true)]);
}

#[test]
fn semua_cooldown_tetap_menghasilkan_urutan_percobaan() {
    let k = Kesehatan::default();
    let t0 = Instant::now();
    k.gagal("a", dtk(30), t0);
    let hasil = k.urutan(&["a"], t0 + dtk(1));
    assert_eq!(hasil, vec![(0, true)], "tidak pernah kosong: tetap dicoba (probe)");
}

#[test]
fn sukses_menghapus_keadaan_dan_status_dilaporkan() {
    let k = Kesehatan::default();
    let t0 = Instant::now();
    k.gagal("a", dtk(30), t0);
    k.gagal("a", dtk(30), t0);
    let s = k.status("a", t0 + dtk(10));
    assert_eq!(s.gagal_beruntun, 2);
    assert_eq!(s.sisa_cooldown, Some(dtk(20)));
    assert_eq!(k.status("a", t0 + dtk(31)).sisa_cooldown, None);

    k.sukses("a");
    assert_eq!(k.status("a", t0).gagal_beruntun, 0);
    assert_eq!(k.urutan(&["a"], t0), vec![(0, false)]);
}
