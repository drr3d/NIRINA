//! Integrasi dengan platform (`/platform/config.json`, CONTRACT.md platform): setting yang diisi admin di halaman platform
//! dipakai sebagai key provider dan token admin; tanpa file platform (laptop) perilaku lama (env) tidak berubah.

use std::{
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

use nigate::{AppState, config::Config, keys::KeyStore, platform::SumberPlatform};
use serde_json::json;

fn folder(nama: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!("nigate-platform-{nama}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&p);
    std::fs::create_dir_all(&p).unwrap();
    p
}

fn tulis_platform(path: &PathBuf, settings: serde_json::Value) {
    let isi = json!({"schema_version": 1, "team": "nigate", "revision": "r1", "settings": settings, "platform": {"proxy_token": "t"}});
    std::fs::write(path, serde_json::to_vec_pretty(&isi).unwrap()).unwrap();
}

const TOML: &str = r#"
[auth]
required = false
[admin]
listen = "127.0.0.1:0"
[[model]]
alias = "m1"
  [[model.upstream]]
  name = "cerebras"
  base_url = "http://127.0.0.1:9/v1"
  model = "x"
  api_key_env = "CEREBRAS_API_KEY"
"#;

fn key_upstream(c: &Config) -> Option<String> {
    c.models["m1"].upstreams[0].api_key.clone()
}

fn tanpa_env(_: &str) -> Option<String> {
    None
}

#[test]
fn tanpa_file_platform_perilaku_env_tidak_berubah() {
    let d = folder("tanpa");
    let sumber = SumberPlatform::baru(d.join("config.json"));
    assert!(sumber.baca().unwrap().is_none(), "file tidak ada = belum di-setup, bukan galat");
    assert!(sumber.settings().is_none());
    let env = |n: &str| (n == "CEREBRAS_API_KEY").then(|| "dari-env".to_string());
    let c = Config::from_toml_str(TOML, &sumber.pencari_dengan(env)).unwrap();
    assert_eq!(key_upstream(&c).as_deref(), Some("dari-env"));
}

#[test]
fn setting_platform_mengisi_key_provider_dan_token_admin() {
    let d = folder("isi");
    let p = d.join("config.json");
    tulis_platform(&p, json!({"CEREBRAS_API_KEY": "csk-platform", "NIGATE_ADMIN_TOKEN": "a".repeat(32)}));
    let sumber = SumberPlatform::baru(&p);
    let c = Config::from_toml_str(TOML, &sumber.pencari_dengan(tanpa_env)).unwrap();
    assert_eq!(key_upstream(&c).as_deref(), Some("csk-platform"));
    assert_eq!(c.admin_token.as_deref(), Some("a".repeat(32).as_str()));
}

#[test]
fn platform_menang_atas_env_tetapi_nilai_kosong_tidak_menimpa() {
    let d = folder("prioritas");
    let p = d.join("config.json");
    let env = |n: &str| (n == "CEREBRAS_API_KEY").then(|| "dari-env".to_string());

    tulis_platform(&p, json!({"CEREBRAS_API_KEY": "dari-platform"}));
    let c = Config::from_toml_str(TOML, &SumberPlatform::baru(&p).pencari_dengan(env)).unwrap();
    assert_eq!(key_upstream(&c).as_deref(), Some("dari-platform"));

    for kosong in [json!(""), json!("   "), json!(null)] {
        tulis_platform(&p, json!({ "CEREBRAS_API_KEY": kosong }));
        let c = Config::from_toml_str(TOML, &SumberPlatform::baru(&p).pencari_dengan(env)).unwrap();
        assert_eq!(key_upstream(&c).as_deref(), Some("dari-env"), "setting kosong ({kosong}) tidak boleh menghapus env");
    }
}

#[test]
fn tipe_setting_platform_dibaca_sebagai_teks() {
    let d = folder("tipe");
    let p = d.join("config.json");
    tulis_platform(&p, json!({"RUST_LOG": "debug", "PORT": 9000, "AKTIF": true, "OBJEK": {"x": 1}}));
    let s = SumberPlatform::baru(&p).settings().unwrap();
    assert_eq!(s.get("RUST_LOG").map(String::as_str), Some("debug"));
    assert_eq!(s.get("PORT").map(String::as_str), Some("9000"));
    assert_eq!(s.get("AKTIF").map(String::as_str), Some("true"));
    assert!(!s.contains_key("OBJEK"), "nilai bertingkat bukan setting skalar dan diabaikan");
}

#[test]
fn file_rusak_atau_skema_lain_ditolak_dengan_jelas() {
    let d = folder("rusak");
    let p = d.join("config.json");
    let sumber = SumberPlatform::baru(&p);
    for isi in [r#"{"schema_version": 1, "settings": "#, r#"{"schema_version": 2, "settings": {}}"#, r#"{"settings": {}}"#, "[]"] {
        std::fs::write(&p, isi).unwrap();
        let e = sumber.baca().expect_err(isi);
        assert!(format!("{e:#}").contains("config.json"), "pesan galat harus menyebut file: {e:#}");
    }
}

#[test]
fn file_rusak_setelah_versi_valid_tetap_memakai_nilai_terakhir() {
    let d = folder("terakhir");
    let p = d.join("config.json");
    tulis_platform(&p, json!({"CEREBRAS_API_KEY": "versi-valid"}));
    let sumber = SumberPlatform::baru(&p);
    assert_eq!(sumber.settings().unwrap()["CEREBRAS_API_KEY"], "versi-valid");
    std::fs::write(&p, "{rusak").unwrap();
    assert_eq!(sumber.settings().unwrap()["CEREBRAS_API_KEY"], "versi-valid", "platform menulis ulang file; jangan kehilangan key");
    // Rusak sejak awal (belum pernah ada versi valid): tanpa setting platform, env tetap dipakai.
    let baru = SumberPlatform::baru(&p);
    assert!(baru.settings().is_none());
}

#[test]
fn perubahan_file_platform_terdeteksi() {
    let d = folder("ubah");
    let p = d.join("config.json");
    tulis_platform(&p, json!({"CEREBRAS_API_KEY": "k1"}));
    let sumber = SumberPlatform::baru(&p);
    assert!(!sumber.berubah(), "belum ada perubahan sejak dibuat");
    tulis_platform(&p, json!({"CEREBRAS_API_KEY": "k2-lebih-panjang"}));
    assert!(sumber.berubah());
    assert!(!sumber.berubah(), "perubahan yang sama hanya dilaporkan sekali");
    std::fs::remove_file(&p).unwrap();
    assert!(sumber.berubah(), "file dihapus juga perubahan");
}

#[tokio::test]
async fn gateway_menerapkan_key_baru_dari_platform_tanpa_restart() {
    let d = folder("pantau");
    let (p, toml) = (d.join("config.json"), d.join("nigate.toml"));
    std::fs::write(&toml, TOML).unwrap();
    tulis_platform(&p, json!({"CEREBRAS_API_KEY": "k-lama", "NIGATE_ADMIN_TOKEN": "a".repeat(32)}));
    let sumber = Arc::new(SumberPlatform::baru(&p));
    let toml_str = toml.to_str().unwrap();
    let cfg = Config::from_file_dengan(toml_str, &sumber.pencari_dengan(tanpa_env)).unwrap();
    let s = AppState::new(cfg, Arc::new(KeyStore::open_memory().unwrap()))
        .unwrap()
        .dengan_config_path(toml_str)
        .dengan_platform(sumber.clone());
    let pantau = nigate::platform::pantau(s.clone(), Duration::from_millis(50));

    tulis_platform(&p, json!({"CEREBRAS_API_KEY": "k-baru-dari-platform", "NIGATE_ADMIN_TOKEN": "b".repeat(32)}));
    let mulai = Instant::now();
    while key_upstream(&s.runtime().config).as_deref() != Some("k-baru-dari-platform") {
        assert!(mulai.elapsed() < Duration::from_secs(3), "key baru tidak diterapkan");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    // Token admin terikat listener yang sudah berjalan: berlaku setelah restart, bukan diganti diam-diam.
    assert_eq!(s.runtime().config.admin_token.as_deref(), Some("a".repeat(32).as_str()));

    // File rusak di tengah jalan: config yang berjalan tidak berubah.
    std::fs::write(&p, "{rusak").unwrap();
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(key_upstream(&s.runtime().config).as_deref(), Some("k-baru-dari-platform"));
    pantau.abort();
}
