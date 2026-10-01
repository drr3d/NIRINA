use std::path::PathBuf;

use nigate::keys::{KeyStore, hash_key};

fn db_sementara(nama: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!("nigate-tes-{nama}-{}.db", std::process::id()));
    let _ = std::fs::remove_file(&p);
    p
}

#[test]
fn set_limits_tersimpan_terbaca_dan_bisa_dihapus() {
    let s = KeyStore::open_memory().unwrap();
    let (_, token) = s.create("a").unwrap();
    assert_eq!(s.list().unwrap()[0].rpm, None);

    assert!(s.set_limits("a", Some(60), Some(100_000)).unwrap());
    let info = s.authenticate(&token).unwrap();
    assert_eq!((info.rpm, info.tpm), (Some(60), Some(100_000)));

    assert!(s.set_limits("a", None, Some(5)).unwrap());
    let info = s.authenticate(&token).unwrap();
    assert_eq!((info.rpm, info.tpm), (None, Some(5)));
    assert!(!s.set_limits("tidak-ada", Some(1), None).unwrap());
}

#[test]
fn set_limits_menolak_nol() {
    let s = KeyStore::open_memory().unwrap();
    s.create("a").unwrap();
    assert!(s.set_limits("a", Some(0), None).is_err());
    assert!(s.set_limits("a", None, Some(0)).is_err());
}

#[test]
fn migrasi_v1_ke_v2_mempertahankan_key_lama() {
    let p = db_sementara("migrasi");
    let token = "ngk_kunci-lama-dari-versi-1";
    {
        let c = rusqlite::Connection::open(&p).unwrap();
        c.execute_batch(
            "CREATE TABLE api_keys (
                 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, key_hash TEXT NOT NULL UNIQUE,
                 key_prefix TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
             );
             PRAGMA user_version = 1;",
        )
        .unwrap();
        c.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix, active, created_at) VALUES ('lama', ?1, 'ngk_kunc', 1, 1)",
            [hash_key(token)],
        )
        .unwrap();
    }
    let s = KeyStore::open(p.to_str().unwrap()).unwrap();
    let info = s.authenticate(token).expect("key lama harus tetap berlaku");
    assert_eq!((info.name.as_str(), info.rpm, info.tpm), ("lama", None, None));
    assert!(s.set_limits("lama", Some(10), None).unwrap());
    drop(s);

    let version: i64 = rusqlite::Connection::open(&p).unwrap().query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
    assert_eq!(version, 2);
    let _ = std::fs::remove_file(&p);
}
