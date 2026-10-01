use std::path::PathBuf;

use nigate::keys::{KeyStore, hash_key};

fn db_sementara(nama: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!("nigate-tes-{nama}-{}.db", std::process::id()));
    let _ = std::fs::remove_file(&p);
    p
}

#[test]
fn create_menghasilkan_key_unik_berawalan_ngk_dan_bisa_diautentikasi() {
    let s = KeyStore::open_memory().unwrap();
    let (a, ta) = s.create("a").unwrap();
    let (_, tb) = s.create("b").unwrap();
    assert!(ta.starts_with("ngk_") && ta.len() == 4 + 64);
    assert_ne!(ta, tb);
    assert!(ta.starts_with(&a.prefix));
    assert_eq!(s.authenticate(&ta).unwrap().name, "a");
    assert!(s.authenticate("ngk_tidak-ada").is_none());
}

#[test]
fn nama_ganda_dan_nama_buruk_ditolak() {
    let s = KeyStore::open_memory().unwrap();
    s.create("a").unwrap();
    assert!(s.create("a").unwrap_err().to_string().contains("sudah dipakai"));
    for buruk in ["", "spasi di sini", "a/b", &"x".repeat(65)] {
        assert!(s.create(buruk).is_err(), "harus ditolak: {buruk:?}");
    }
}

#[test]
fn key_asli_tidak_tersimpan_di_database() {
    let p = db_sementara("plain");
    let s = KeyStore::open(p.to_str().unwrap()).unwrap();
    let (_, token) = s.create("a").unwrap();
    drop(s);
    let mentah = std::fs::read(&p).unwrap();
    let ada = |k: &[u8]| mentah.windows(k.len()).any(|w| w == k);
    assert!(!ada(token.as_bytes()), "key asli tidak boleh ada di file DB");
    assert!(ada(hash_key(&token).as_bytes()), "hash harus ada");
    let _ = std::fs::remove_file(&p);
}

#[test]
fn set_active_pada_nama_tak_ada_mengembalikan_false() {
    let s = KeyStore::open_memory().unwrap();
    assert!(!s.set_active("tidak-ada", false).unwrap());
}

#[test]
fn perubahan_dari_proses_lain_terdeteksi_lewat_refresh() {
    let p = db_sementara("dua-koneksi");
    let server = KeyStore::open(p.to_str().unwrap()).unwrap();
    let cli = KeyStore::open(p.to_str().unwrap()).unwrap();

    let (_, token) = cli.create("dari-cli").unwrap();
    assert!(server.authenticate(&token).is_none(), "belum dimuat ulang");
    server.refresh_if_changed().unwrap();
    assert_eq!(server.authenticate(&token).unwrap().name, "dari-cli");

    cli.set_active("dari-cli", false).unwrap();
    server.refresh_if_changed().unwrap();
    assert!(server.authenticate(&token).is_none(), "pencabutan harus terbaca server");
    drop((server, cli));
    let _ = std::fs::remove_file(&p);
}

#[test]
fn key_bertahan_setelah_buka_ulang_dan_skema_terlalu_baru_ditolak() {
    let p = db_sementara("buka-ulang");
    let token = {
        let s = KeyStore::open(p.to_str().unwrap()).unwrap();
        s.create("a").unwrap().1
    };
    assert!(KeyStore::open(p.to_str().unwrap()).unwrap().authenticate(&token).is_some());

    rusqlite::Connection::open(&p).unwrap().execute_batch("PRAGMA user_version = 99;").unwrap();
    let galat = KeyStore::open(p.to_str().unwrap()).err().expect("harus gagal");
    assert!(galat.to_string().contains("lebih baru"));
    let _ = std::fs::remove_file(&p);
}
