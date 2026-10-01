//! Korpus tes guardrail. Semua "rahasia" di sini PALSU dan dirakit dari potongan supaya file ini sendiri tidak
//! memicu pemindai secret. Korpus negatif menjaga false positive tetap terkendali.

use std::collections::HashMap;

use nigate::config::Config;
use nigate::guardrail::{Guardrail, GuardrailCfg, Laporan, Mode, NAMA_ENTROPI, entropi_shannon};
use serde_json::{Value, json};

fn g() -> Guardrail {
    Guardrail::baru(&GuardrailCfg::default()).unwrap()
}

fn periksa(g: &Guardrail, teks: &str) -> (String, Laporan) {
    let mut lap = Laporan::default();
    let hasil = g.periksa(teks, &mut lap).into_owned();
    (hasil, lap)
}

/// Karakter pseudo-acak deterministik (tanpa dependency) untuk merakit token palsu.
fn acak(n: usize, alfabet: &str, benih: u64) -> String {
    let a: Vec<char> = alfabet.chars().collect();
    let mut x = benih.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
    (0..n)
        .map(|_| {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            a[((x >> 33) as usize) % a.len()]
        })
        .collect()
}

const ALNUM: &str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

// ---------- korpus POSITIF: harus terdeteksi ----------

fn korpus_positif() -> Vec<(&'static str, String)> {
    let b64 = |n, s| acak(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/", s);
    vec![
        (
            "private_key",
            format!(
                "kunci:\n-----BEGIN RSA {}-----\n{}\n{}\n-----END RSA {}-----\nselesai",
                "PRIVATE KEY",
                b64(64, 1),
                b64(64, 2),
                "PRIVATE KEY"
            ),
        ),
        ("private_key", format!("-----BEGIN {}-----\n{}", "OPENSSH PRIVATE KEY", b64(70, 3))),
        ("aws_access_key", format!("AWS id {}{}", "AKIA", acak(16, "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", 4))),
        ("aws_secret_key", format!("aws_secret_access_key = \"{}\"", b64(40, 5).replace('+', "A"))),
        ("github_token", format!("token {}_{}", "ghp", acak(36, ALNUM, 6))),
        ("github_token", format!("{}_{}", "github_pat", acak(40, ALNUM, 7))),
        ("sk_api_key", format!("OPENAI={}-{}", "sk", acak(48, ALNUM, 8))),
        ("sk_api_key", format!("{}-{}-{}", "sk", "ant-api03", acak(60, ALNUM, 9))),
        ("cerebras_key", format!("{}-{}", "csk", acak(48, "abcdefghijklmnopqrstuvwxyz0123456789", 10))),
        ("groq_key", format!("{}_{}", "gsk", acak(52, ALNUM, 11))),
        ("nigate_key", format!("{}_{}", "ngk", acak(64, "0123456789abcdef", 12))),
        ("slack_token", format!("{}-{}-{}", "xoxb", "123456789012", acak(24, ALNUM, 13))),
        ("google_api_key", format!("key={}{}", "AIza", acak(35, ALNUM, 14))),
        ("stripe_key", format!("{}_{}_{}", "sk", "live", acak(30, ALNUM, 15))),
        (
            "jwt",
            format!("{}.{}.{}", "eyJ".to_string() + &acak(20, ALNUM, 16), "eyJ".to_string() + &acak(30, ALNUM, 17), acak(43, ALNUM, 18)),
        ),
        ("bearer_token", format!("Authorization: Bearer {}", acak(40, ALNUM, 19))),
        ("url_credentials", "postgres://app_user:Sup3rRahasia!Pw@db.example.invalid:5432/appdb".to_string()),
        ("secret_assignment", "DB_PASSWORD=example-pw-1234".to_string()),
        ("secret_assignment", r#"{"api_key": "abcd1234efgh5678"}"#.to_string()),
        ("secret_assignment", "client_secret: 9f8e7d6c5b4a".to_string()),
        (NAMA_ENTROPI, format!("token acak: {}", acak(44, ALNUM, 20))),
    ]
}

#[test]
fn semua_korpus_positif_terdeteksi_dan_diredaksi() {
    let g = g();
    for (aturan, teks) in korpus_positif() {
        let (hasil, lap) = periksa(&g, &teks);
        assert!(lap.temuan.contains_key(aturan), "aturan '{aturan}' tidak terpicu untuk: {teks:?} (temuan: {:?})", lap.temuan);
        assert!(hasil.contains("[REDACTED:"), "tidak ada penggantian untuk '{aturan}': {hasil:?}");
        assert!(lap.berubah || !hasil.is_empty());
        // idempoten: hasil redaksi tidak memicu apa pun lagi
        let (ulang, lap2) = periksa(&g, &hasil);
        assert!(lap2.temuan.is_empty(), "hasil redaksi masih memicu {:?}: {hasil:?}", lap2.temuan);
        assert_eq!(ulang, hasil);
    }
}

#[test]
fn redaksi_mempertahankan_teks_di_sekitarnya() {
    let g = g();
    let kunci = format!("{}-{}", "sk", acak(40, ALNUM, 30));
    let (hasil, lap) = periksa(&g, &format!("Tolong pakai kunci {kunci} untuk panggilan ini, terima kasih."));
    assert_eq!(hasil, "Tolong pakai kunci [REDACTED:sk_api_key] untuk panggilan ini, terima kasih.");
    assert_eq!(lap.temuan["sk_api_key"], 1);
    assert!(!hasil.contains(&kunci));
}

#[test]
fn hanya_nilai_yang_diganti_bukan_nama_variabel() {
    let (hasil, _) = periksa(&g(), "DB_PASSWORD=example-pw-1234 dan host=db1");
    assert_eq!(hasil, "DB_PASSWORD=[REDACTED:secret_assignment] dan host=db1");
    let (hasil, _) = periksa(&g(), "postgres://app_user:Sup3rRahasia!Pw@db.example.invalid:5432/x");
    assert_eq!(hasil, "postgres://app_user:[REDACTED:url_credentials]@db.example.invalid:5432/x");
}

#[test]
fn beberapa_rahasia_dalam_satu_teks_dan_temuan_tumpang_tindih() {
    let g = g();
    let a = format!("{}_{}", "ghp", acak(36, ALNUM, 31));
    let b = format!("{}{}", "AKIA", acak(16, "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", 32));
    let (hasil, lap) = periksa(&g, &format!("a={a}; b={b}"));
    assert_eq!(lap.total(), 2);
    assert_eq!(hasil, "a=[REDACTED:github_token]; b=[REDACTED:aws_access_key]");

    // blok private key berisi baris beentropi tinggi: harus dihitung SATU temuan, bukan banyak
    let pem =
        format!("-----BEGIN {}-----\n{}\n{}\n-----END {}-----", "PRIVATE KEY", acak(64, ALNUM, 33), acak(64, ALNUM, 34), "PRIVATE KEY");
    let (hasil, lap) = periksa(&g, &pem);
    assert_eq!(lap.total(), 1, "temuan: {:?}", lap.temuan);
    assert_eq!(hasil, "[REDACTED:private_key]");
}

// ---------- korpus NEGATIF: tidak boleh terdeteksi (kontrol false positive) ----------

#[test]
fn korpus_negatif_tidak_menghasilkan_temuan() {
    let g = g();
    let sha256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";
    let negatif = [
        "Halo, tolong ringkas penjualan toko bulan Agustus dan bandingkan dengan bulan Juli.",
        "SELECT id, SUM(amount) AS total FROM orders WHERE tanggal >= '2026-08-01' GROUP BY id ORDER BY 2 DESC;",
        "ID pesanan: 550e8400-e29b-41d4-a716-446655440000 (UUID)",
        &format!("sha256: {sha256}"),
        "commit 3b18e512dba79e4c8300dd08aeb37f8e728b8dad ditambahkan kemarin",
        "md5 d41d8cd98f00b204e9800998ecf8427e",
        "https://dashboard.example.com/laporan/harian?toko=utama&periode=2026-08",
        "https://pengguna@example.com/path dan ftp://anon@host tanpa password",
        "password = get_password()  # ambil dari vault",
        "api_key=None",
        "API_KEY=${OPENAI_API_KEY}",
        "secret = <isi_rahasia_anda>",
        "password: ********",
        "password=changeme",
        "Gunakan Bearer authentication untuk memanggil endpoint ini.",
        "Header: Authorization: Bearer <token>",
        "file: /var/lib/postgresql/data/base/16384/2619 dan /usr/local/lib/python3.11/site-packages/langchain_core",
        "NamaKelasYangSangatPanjangDanDeskriptifUntukPengujianEntropiRendah dan nama_fungsi_python_yang_sangat_panjang_sekali_ok",
        "node-01-a.region-1.compute.example.invalid port 5432 user readonly",
        "Email: jane.doe@example.com, telp 0812-3456-7890, kode pajak 12-3456789",
        "| produk | terjual | konversi |\n|---|---|---|\n| Produk A | 12.345 | 8,2% |\n| Produk B | 45.678 | 6,9% |",
        "{\"shop\": \"SHOP-01\", \"orders\": 1234, \"date\": \"2026-08-15\", \"channel\": \"online\"}",
        "1234567890123456789012345678901234567890123456789012345678901234567890",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "Rp 1.234.567,00 dari 9.876.543 transaksi selama 365 hari terakhir",
        "call_id: call_abc123 tool_call_id: call_0_a1b2c3",
        "sk-learn adalah pustaka; gunakan `sk-` sebagai awalan saja",
        "Contoh kunci: AKIA... (dipotong) dan ghp_... (dipotong)",
    ];
    for teks in negatif {
        let (hasil, lap) = periksa(&g, teks);
        assert!(lap.temuan.is_empty(), "FALSE POSITIVE {:?} pada: {teks:?}", lap.temuan);
        assert_eq!(hasil, teks);
    }
}

#[test]
fn entropi_membedakan_acak_dari_teks_biasa() {
    assert!(entropi_shannon(acak(48, ALNUM, 40).as_bytes()) > 4.5);
    assert!(entropi_shannon(b"hello hello hello hello") < 3.0);
    assert!(entropi_shannon(b"9f86d081884c7d659a2feaa0c55ad015") <= 4.0, "hex maksimal 4 bit/karakter");
}

// ---------- mode ----------

#[test]
fn log_only_menghitung_tanpa_mengubah() {
    let cfg = GuardrailCfg { mode: Mode::LogOnly, ..GuardrailCfg::default() };
    let g = Guardrail::baru(&cfg).unwrap();
    let teks = format!("kunci {}_{}", "ghp", acak(36, ALNUM, 41));
    let (hasil, lap) = periksa(&g, &teks);
    assert_eq!(hasil, teks);
    assert_eq!(lap.total(), 1);
    assert!(!lap.berubah && lap.diblok.is_empty());
}

#[test]
fn block_menandai_aturan_yang_memblokir() {
    let cfg = GuardrailCfg { mode: Mode::Block, ..GuardrailCfg::default() };
    let g = Guardrail::baru(&cfg).unwrap();
    let (hasil, lap) = periksa(&g, &format!("kunci {}_{}", "ghp", acak(36, ALNUM, 42)));
    assert_eq!(lap.diblok, vec!["github_token".to_string()]);
    assert!(!hasil.contains("REDACTED"), "mode block tidak mengubah teks; penolakan dilakukan pemanggil");
}

#[test]
fn aksi_per_aturan_menimpa_mode_global() {
    let mut aksi = HashMap::new();
    aksi.insert("private_key".to_string(), Mode::Block);
    aksi.insert(NAMA_ENTROPI.to_string(), Mode::LogOnly);
    let g = Guardrail::baru(&GuardrailCfg { aksi, ..GuardrailCfg::default() }).unwrap();

    let (_, lap) = periksa(&g, &format!("-----BEGIN {}-----\n{}", "PRIVATE KEY", acak(60, ALNUM, 43)));
    assert_eq!(lap.diblok, vec!["private_key".to_string()]);
    let (hasil, lap) = periksa(&g, &format!("x {}", acak(44, ALNUM, 44)));
    assert_eq!((lap.total(), lap.berubah), (1, false), "entropi log_only: dihitung, tidak diubah");
    assert!(!hasil.contains("REDACTED"));
    let (hasil, _) = periksa(&g, &format!("{}_{}", "ghp", acak(36, ALNUM, 45)));
    assert!(hasil.contains("REDACTED:github_token"), "aturan lain tetap redact");
}

#[test]
fn entropi_bisa_dimatikan_dan_ambangnya_diatur() {
    let token = acak(44, ALNUM, 46);
    let mati = Guardrail::baru(&GuardrailCfg { entropi: false, ..GuardrailCfg::default() }).unwrap();
    assert!(periksa(&mati, &token).1.temuan.is_empty());
    let ketat = Guardrail::baru(&GuardrailCfg { entropi_ambang: 6.0, ..GuardrailCfg::default() }).unwrap();
    assert!(periksa(&ketat, &token).1.temuan.is_empty(), "ambang 6.0 tidak tercapai");
    let pendek = Guardrail::baru(&GuardrailCfg { entropi_min_panjang: 64, ..GuardrailCfg::default() }).unwrap();
    assert!(periksa(&pendek, &token).1.temuan.is_empty(), "token 44 < min 64");
}

#[test]
fn aturan_kustom_dan_validasi_nama() {
    use nigate::guardrail::AturanKustom;
    let cfg = GuardrailCfg {
        kustom: vec![AturanKustom { nama: "id_internal".into(), pola: r"TKT-\d{8}".into(), mode: Some(Mode::Block) }],
        ..GuardrailCfg::default()
    };
    let g = Guardrail::baru(&cfg).unwrap();
    let (_, lap) = periksa(&g, "referensi TKT-12345678 milik pelanggan");
    assert_eq!(lap.diblok, vec!["id_internal".to_string()]);

    let salah = |c: GuardrailCfg| Guardrail::baru(&c).err().map(|e| format!("{e:#}")).unwrap_or_default();
    let mut aksi = HashMap::new();
    aksi.insert("aturan_typo".to_string(), Mode::Block);
    assert!(salah(GuardrailCfg { aksi, ..GuardrailCfg::default() }).contains("tidak ada"), "typo nama di aksi harus ditolak");
    let dobel = vec![AturanKustom { nama: "jwt".into(), pola: "x".into(), mode: None }];
    assert!(salah(GuardrailCfg { kustom: dobel, ..GuardrailCfg::default() }).contains("sudah dipakai"));
    let rusak = vec![AturanKustom { nama: "rusak".into(), pola: "(".into(), mode: None }];
    assert!(salah(GuardrailCfg { kustom: rusak, ..GuardrailCfg::default() }).contains("tidak valid"));
}

// ---------- pemindaian struktur JSON ----------

#[test]
fn request_hanya_kolom_isi_yang_dipindai() {
    let g = g();
    let rahasia = format!("{}_{}", "ghp", acak(36, ALNUM, 50));
    let id_acak = acak(48, ALNUM, 51);
    let mut req = json!({
        "model": id_acak,
        "messages": [
            {"role": "system", "content": "Kamu asisten."},
            {"role": "user", "content": format!("pakai {rahasia}")},
            {"role": "user", "content": [
                {"type": "text", "text": format!("teks {rahasia}")},
                {"type": "image_url", "image_url": {"url": format!("data:image/png;base64,{}", acak(200, ALNUM, 52))}}
            ]},
            {"role": "assistant", "content": null, "tool_calls": [
                {"id": id_acak, "type": "function", "function": {"name": "cari", "arguments": format!("{{\"token\": \"{rahasia}\"}}")}}
            ]},
            {"role": "tool", "tool_call_id": id_acak, "content": format!("hasil tool memuat {rahasia}")}
        ]
    });
    let mut lap = Laporan::default();
    g.pindai_request(&mut req, &mut lap);

    assert_eq!(lap.temuan["github_token"], 4, "user string + bagian teks + argumen tool_call + pesan tool");
    let s = req.to_string();
    assert!(!s.contains(&rahasia));
    assert_eq!(req["model"], id_acak.as_str(), "model tidak disentuh");
    assert_eq!(req["messages"][3]["tool_calls"][0]["id"], id_acak.as_str(), "id tool call tidak disentuh");
    assert_eq!(req["messages"][4]["tool_call_id"], id_acak.as_str());
    assert!(
        req["messages"][2]["content"][1]["image_url"]["url"].as_str().unwrap().starts_with("data:image/png;base64,"),
        "gambar tidak dipindai"
    );
    let args: Value = serde_json::from_str(req["messages"][3]["tool_calls"][0]["function"]["arguments"].as_str().unwrap())
        .expect("argumen tetap JSON valid");
    assert_eq!(args["token"], "[REDACTED:github_token]");
}

#[test]
fn respons_dipindai_pada_message_dan_tool_calls() {
    let g = g();
    let rahasia = format!("{}-{}", "sk", acak(40, ALNUM, 53));
    let mut resp = json!({
        "id": "chatcmpl-x", "usage": {"total_tokens": 5},
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant",
            "content": format!("kuncinya {rahasia}"),
            "reasoning_content": format!("pikir {rahasia}"),
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": format!("{{\"k\":\"{rahasia}\"}}")}}]
        }}]
    });
    let mut lap = Laporan::default();
    g.pindai_respons(&mut resp, &mut lap);
    assert_eq!(lap.total(), 3);
    assert!(!resp.to_string().contains(&rahasia));
    assert_eq!(resp["usage"]["total_tokens"], 5);
    assert_eq!(resp["choices"][0]["finish_reason"], "stop");
}

#[test]
fn arah_bisa_dimatikan_terpisah() {
    let rahasia = format!("{}_{}", "ghp", acak(36, ALNUM, 54));
    let hanya_respons = Guardrail::baru(&GuardrailCfg { scan_request: false, ..GuardrailCfg::default() }).unwrap();
    let mut req = json!({"messages": [{"role": "user", "content": rahasia.clone()}]});
    let mut lap = Laporan::default();
    hanya_respons.pindai_request(&mut req, &mut lap);
    assert_eq!(lap.total(), 0);
    assert_eq!(req["messages"][0]["content"], rahasia.as_str());

    let mati = Guardrail::baru(&GuardrailCfg { aktif: false, ..GuardrailCfg::default() }).unwrap();
    let mut lap = Laporan::default();
    mati.pindai_request(&mut req, &mut lap);
    assert_eq!(lap.total(), 0);
    assert!(!mati.aktif_request() && !mati.aktif_response());
}

#[test]
fn teks_besar_dipindai_cepat() {
    let g = g();
    let besar = "penjualan toko naik 12% dibanding bulan lalu, ".repeat(20_000); // ~900 KB
    let mulai = std::time::Instant::now();
    let (_, lap) = periksa(&g, &besar);
    assert!(lap.temuan.is_empty());
    assert!(mulai.elapsed() < std::time::Duration::from_secs(5), "terlalu lambat: {:?}", mulai.elapsed());
}

// ---------- konfigurasi TOML ----------

#[test]
fn config_guardrail_default_dan_kustom() {
    let c = Config::from_toml_str("", &|_| None).unwrap();
    assert!(c.guardrail.aktif && c.guardrail.mode == Mode::Redact && c.guardrail.entropi);

    let toml = r#"
[guardrail]
mode = "log_only"
entropy_threshold = 4.8
[guardrail.aksi]
private_key = "block"
[[guardrail.rule]]
name = "id_internal"
pattern = "TKT-[0-9]{8}"
mode = "block"
"#;
    let c = Config::from_toml_str(toml, &|_| None).unwrap();
    assert_eq!(c.guardrail.mode, Mode::LogOnly);
    assert_eq!(c.guardrail.aksi["private_key"], Mode::Block);
    assert_eq!(c.guardrail.kustom[0].mode, Some(Mode::Block));
}

#[test]
fn config_guardrail_menolak_yang_salah() {
    let buruk = [
        "[guardrail]\nmode = \"ngawur\"\n",
        "[guardrail]\nentropy_min_length = 4\n",
        "[guardrail]\nentropy_threshold = 9.0\n",
        "[guardrail.aksi]\nprivate_kee = \"block\"\n",
        "[guardrail.aksi]\njwt = \"hapus\"\n",
        "[[guardrail.rule]]\nname = \"Nama Besar\"\npattern = \"x\"\n",
        "[[guardrail.rule]]\nname = \"rusak\"\npattern = \"(\"\n",
        "[guardrail]\nenabeld = true\n",
    ];
    for b in buruk {
        assert!(Config::from_toml_str(b, &|_| None).is_err(), "harus ditolak: {b:?}");
    }
}
