use std::{
    borrow::Cow,
    collections::{BTreeMap, HashMap, HashSet},
};

use anyhow::{Context, Result, bail};
use regex::{Regex, RegexBuilder, RegexSet};
use serde_json::Value;

/// Nama aturan untuk detektor entropi (bisa diberi aksi lewat `[guardrail.aksi]` seperti aturan lain).
pub const NAMA_ENTROPI: &str = "high_entropy";
const SENTINEL_ENTROPI: usize = usize::MAX;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// Ganti temuan dengan `[REDACTED:<aturan>]` lalu teruskan.
    Redact,
    /// Tolak seluruh request/respons.
    Block,
    /// Hanya hitung dan catat; isi tidak diubah (untuk masa uji).
    LogOnly,
}

impl Mode {
    pub fn teks(self) -> &'static str {
        match self {
            Self::Redact => "redact",
            Self::Block => "block",
            Self::LogOnly => "log_only",
        }
    }

    pub fn dari_teks(t: &str) -> Result<Self> {
        match t {
            "redact" => Ok(Self::Redact),
            "block" => Ok(Self::Block),
            "log_only" => Ok(Self::LogOnly),
            lain => bail!("mode guardrail '{lain}' tidak dikenal (redact | block | log_only)"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct AturanKustom {
    pub nama: String,
    pub pola: String,
    pub mode: Option<Mode>,
}

#[derive(Debug, Clone)]
pub struct GuardrailCfg {
    pub aktif: bool,
    pub mode: Mode,
    pub scan_request: bool,
    pub scan_response: bool,
    pub entropi: bool,
    pub entropi_min_panjang: usize,
    pub entropi_ambang: f64,
    /// Mode khusus per nama aturan (menimpa mode global).
    pub aksi: HashMap<String, Mode>,
    pub kustom: Vec<AturanKustom>,
}

impl Default for GuardrailCfg {
    fn default() -> Self {
        Self {
            aktif: true,
            mode: Mode::Redact,
            scan_request: true,
            scan_response: true,
            entropi: true,
            entropi_min_panjang: 32,
            entropi_ambang: 4.5,
            aksi: HashMap::new(),
            kustom: Vec::new(),
        }
    }
}

type Filter = fn(&str) -> bool;

/// (nama, pola, filter). Bila pola punya grup tangkap, hanya grup 1 yang diganti (mis. nilai setelah `password=`).
const BAWAAN: &[(&str, &str, Option<Filter>)] = &[
    // Tanpa `.*?` malas: pola lama membuat tiap penanda BEGIN tanpa END memindai sampai akhir teks (kuadratik: 1,3 MB = 17 detik).
    // Isi dibatasi ke karakter PEM; deretan 5 tanda hubung (awal "-----END") menghentikannya, lalu END diambil bila ada.
    (
        "private_key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[A-Za-z0-9+/=\s:,.\\]|-{1,4}[A-Za-z0-9+/=\s:,.\\])*(?:-----END [A-Z ]*PRIVATE KEY-----)?",
        Some(pem_cukup),
    ),
    ("aws_access_key", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b", None),
    ("aws_secret_key", r#"(?i)aws.{0,20}?secret.{0,20}?[=:]\s*["']?([A-Za-z0-9/+=]{40})\b"#, None),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b", None),
    ("sk_api_key", r"\bsk-[A-Za-z0-9_-]{20,}", None),
    ("cerebras_key", r"\bcsk-[A-Za-z0-9]{30,}\b", None),
    ("groq_key", r"\bgsk_[A-Za-z0-9]{40,}\b", None),
    ("nigate_key", r"\bngk_[0-9a-f]{64}\b", None),
    ("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}", None),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b", None),
    ("stripe_key", r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{24,}\b", None),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", None),
    ("bearer_token", r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})", Some(token_campuran)),
    ("url_credentials", r#"://[^\s/:@'"]+:([^\s/@'"]{3,})@"#, Some(nilai_wajar)),
    (
        "secret_assignment",
        r#"(?i)(?:password|passwd|secret(?:[_-]?key)?|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key)["']?\s*[:=]\s*["']?([^\s"',;]{6,})"#,
        Some(nilai_wajar),
    ),
];

/// Header PEM saja (mis. disebut dalam teks penjelasan) bukan kunci: wajib ada isi minimal 20 karakter sesudah header.
fn pem_cukup(v: &str) -> bool {
    const PENUTUP: &str = "PRIVATE KEY-----";
    v.find(PENUTUP).is_some_and(|i| v.len() - (i + PENUTUP.len()) >= 20)
}

/// Token yang tampak seperti kredensial: memuat huruf dan angka.
fn token_campuran(v: &str) -> bool {
    v.chars().any(|c| c.is_ascii_digit()) && v.chars().any(|c| c.is_ascii_alphabetic())
}

/// Menyaring nilai yang jelas bukan rahasia: placeholder, ekspresi kode, atau topeng seperti `******`.
fn nilai_wajar(v: &str) -> bool {
    let l = v.to_ascii_lowercase();
    if matches!(
        l.as_str(),
        "none"
            | "null"
            | "true"
            | "false"
            | "undefined"
            | "changeme"
            | "password"
            | "required"
            | "optional"
            | "string"
            | "secret"
            | "example"
            | "redacted"
    ) {
        return false;
    }
    if v.contains(['(', ')', '{', '}', '$', '<', '>', '[', ']', '*']) {
        return false;
    }
    v.chars().collect::<HashSet<_>>().len() > 2
}

struct Aturan {
    nama: String,
    re: Regex,
    grup: usize,
    mode: Mode,
    filter: Option<Filter>,
}

/// Hasil pemindaian satu atau lebih teks.
#[derive(Debug, Default)]
pub struct Laporan {
    /// nama aturan -> jumlah temuan (termasuk yang mode log_only).
    pub temuan: BTreeMap<String, u32>,
    /// Aturan bermode block yang menemukan sesuatu (unik).
    pub diblok: Vec<String>,
    /// true bila ada teks yang benar-benar diubah.
    pub berubah: bool,
}

impl Laporan {
    pub fn total(&self) -> u32 {
        self.temuan.values().sum()
    }
}

pub struct Guardrail {
    aktif: bool,
    scan_request: bool,
    scan_response: bool,
    aturan: Vec<Aturan>,
    set: RegexSet,
    entropi: Option<Entropi>,
}

struct Entropi {
    min_panjang: usize,
    ambang: f64,
    mode: Mode,
}

impl Guardrail {
    pub fn nonaktif() -> Self {
        Self::baru(&GuardrailCfg { aktif: false, ..GuardrailCfg::default() }).expect("konfigurasi bawaan valid")
    }

    pub fn baru(cfg: &GuardrailCfg) -> Result<Self> {
        let mut nama_dikenal: HashSet<String> = BAWAAN.iter().map(|(n, ..)| n.to_string()).collect();
        nama_dikenal.insert(NAMA_ENTROPI.to_string());
        for k in &cfg.kustom {
            if !nama_dikenal.insert(k.nama.clone()) {
                bail!("guardrail.rule '{}': nama sudah dipakai aturan lain", k.nama);
            }
        }
        for nama in cfg.aksi.keys() {
            if !nama_dikenal.contains(nama) {
                bail!("guardrail.aksi: aturan '{nama}' tidak ada (typo?)");
            }
        }
        let mode_untuk = |nama: &str, bawaan_aturan: Option<Mode>| cfg.aksi.get(nama).copied().or(bawaan_aturan).unwrap_or(cfg.mode);

        let mut aturan = Vec::new();
        for (nama, pola, filter) in BAWAAN {
            let re = Regex::new(pola).with_context(|| format!("pola bawaan '{nama}' tidak valid"))?;
            aturan.push(Aturan {
                nama: nama.to_string(),
                grup: if re.captures_len() > 1 { 1 } else { 0 },
                re,
                mode: mode_untuk(nama, None),
                filter: *filter,
            });
        }
        for k in &cfg.kustom {
            let re = RegexBuilder::new(&k.pola)
                .size_limit(1 << 20)
                .build()
                .with_context(|| format!("guardrail.rule '{}': pola regex tidak valid", k.nama))?;
            aturan.push(Aturan {
                nama: k.nama.clone(),
                grup: if re.captures_len() > 1 { 1 } else { 0 },
                re,
                mode: mode_untuk(&k.nama, k.mode),
                filter: None,
            });
        }
        let set = RegexSet::new(aturan.iter().map(|a| a.re.as_str())).context("gagal menyusun himpunan aturan guardrail")?;
        let entropi = cfg.entropi.then(|| Entropi {
            min_panjang: cfg.entropi_min_panjang,
            ambang: cfg.entropi_ambang,
            mode: mode_untuk(NAMA_ENTROPI, None),
        });
        Ok(Self { aktif: cfg.aktif, scan_request: cfg.scan_request, scan_response: cfg.scan_response, aturan, set, entropi })
    }

    pub fn aktif_request(&self) -> bool {
        self.aktif && self.scan_request
    }

    pub fn aktif_response(&self) -> bool {
        self.aktif && self.scan_response
    }

    /// Memindai satu teks. Mengembalikan teks baru hanya bila ada yang diganti (mode redact).
    pub fn periksa<'a>(&self, teks: &'a str, lap: &mut Laporan) -> Cow<'a, str> {
        // (awal, akhir, indeks aturan)
        let mut temu: Vec<(usize, usize, usize)> = Vec::new();
        for idx in self.set.matches(teks).iter() {
            let a = &self.aturan[idx];
            for c in a.re.captures_iter(teks) {
                if let Some(m) = c.get(a.grup).or_else(|| c.get(0))
                    && a.filter.is_none_or(|f| f(m.as_str()))
                {
                    temu.push((m.start(), m.end(), idx));
                }
            }
        }
        if let Some(e) = &self.entropi {
            // Aturan spesifik lebih informatif (nama jelas, cakupan tepat): entropi hanya untuk bagian yang belum tertangkap.
            // Disapu dengan indeks, bukan membandingkan tiap kandidat dengan semua temuan spesifik (kuadratik: 10 MB dengan
            // 250 ribu temuan = 16 detik): urutkan span menurut awal, simpan akhir-terjauh kumulatif, lalu satu pencarian biner
            // per kandidat. Kandidat tumpang tindih bila ada span yang mulai sebelum `t` dan berakhir setelah `s`.
            let mut span: Vec<(usize, usize)> = temu.iter().map(|&(a, b, _)| (a, b)).collect();
            span.sort_unstable();
            let mut akhir_terjauh = Vec::with_capacity(span.len());
            let mut maks = 0usize;
            for &(_, b) in &span {
                maks = maks.max(b);
                akhir_terjauh.push(maks);
            }
            for (s, t) in token_entropi_tinggi(teks, e.min_panjang, e.ambang) {
                let k = span.partition_point(|&(a, _)| a < t);
                if !(k > 0 && akhir_terjauh[k - 1] > s) {
                    temu.push((s, t, SENTINEL_ENTROPI));
                }
            }
        }
        if temu.is_empty() {
            return Cow::Borrowed(teks);
        }

        // Temuan yang saling tumpang tindih: yang mulai lebih awal (lalu lebih panjang) menang.
        temu.sort_by(|a, b| a.0.cmp(&b.0).then(b.1.cmp(&a.1)));
        let mut keluar = String::with_capacity(teks.len());
        let mut kursor = 0usize; // sampai mana teks asli sudah disalin ke `keluar`
        let mut batas = 0usize; // akhir temuan terakhir yang dihitung (untuk membuang yang tumpang tindih)
        let mut ada_ganti = false;
        for (awal, akhir, idx) in temu {
            if awal < batas {
                continue;
            }
            batas = akhir;
            let (nama, mode) = if idx == SENTINEL_ENTROPI {
                (NAMA_ENTROPI, self.entropi.as_ref().map_or(Mode::Redact, |e| e.mode))
            } else {
                (self.aturan[idx].nama.as_str(), self.aturan[idx].mode)
            };
            *lap.temuan.entry(nama.to_string()).or_insert(0) += 1;
            match mode {
                Mode::Redact => {
                    keluar.push_str(&teks[kursor..awal]);
                    keluar.push_str("[REDACTED:");
                    keluar.push_str(nama);
                    keluar.push(']');
                    kursor = akhir;
                    ada_ganti = true;
                }
                Mode::Block => {
                    if !lap.diblok.iter().any(|n| n == nama) {
                        lap.diblok.push(nama.to_string());
                    }
                }
                Mode::LogOnly => {}
            }
        }
        if !ada_ganti {
            return Cow::Borrowed(teks);
        }
        keluar.push_str(&teks[kursor..]);
        lap.berubah = true;
        Cow::Owned(keluar)
    }

    /// Memindai bagian pesan pada body request (`messages[*]`). Mengubah `body` di tempat.
    pub fn pindai_request(&self, body: &mut Value, lap: &mut Laporan) {
        if !self.aktif_request() {
            return;
        }
        if let Some(pesan) = body.get_mut("messages").and_then(Value::as_array_mut) {
            for m in pesan {
                self.pindai_pesan(m, lap);
            }
        }
    }

    /// Memindai respons chat completion (`choices[*].message`). Mengubah `body` di tempat.
    pub fn pindai_respons(&self, body: &mut Value, lap: &mut Laporan) {
        if !self.aktif_response() {
            return;
        }
        if let Some(pilihan) = body.get_mut("choices").and_then(Value::as_array_mut) {
            for p in pilihan {
                if let Some(m) = p.get_mut("message") {
                    self.pindai_pesan(m, lap);
                }
                self.pindai_nilai(p.get_mut("text"), lap);
            }
        }
    }

    // Hanya kolom yang membawa isi yang dipindai. Sengaja TIDAK menyentuh id, tool_call_id, name, role, model, dll:
    // itu berisi string acak yang sah dan mengubahnya merusak pencocokan tool call.
    fn pindai_pesan(&self, m: &mut Value, lap: &mut Laporan) {
        for kunci in ["content", "reasoning_content", "reasoning", "refusal"] {
            self.pindai_nilai(m.get_mut(kunci), lap);
        }
        if let Some(calls) = m.get_mut("tool_calls").and_then(Value::as_array_mut) {
            for c in calls {
                self.pindai_nilai(c.get_mut("function").and_then(|f| f.get_mut("arguments")), lap);
            }
        }
        self.pindai_nilai(m.get_mut("function_call").and_then(|f| f.get_mut("arguments")), lap);
    }

    /// Nilai berupa string, atau array bagian ber-field `text` (bagian gambar dilewati).
    fn pindai_nilai(&self, v: Option<&mut Value>, lap: &mut Laporan) {
        match v {
            Some(Value::String(s)) => self.pindai_string(s, lap),
            Some(Value::Array(bagian)) => {
                for b in bagian {
                    if let Some(Value::String(s)) = b.get_mut("text") {
                        self.pindai_string(s, lap);
                    }
                }
            }
            _ => {}
        }
    }

    fn pindai_string(&self, s: &mut String, lap: &mut Laporan) {
        let baru = match self.periksa(s, lap) {
            Cow::Owned(n) => Some(n),
            Cow::Borrowed(_) => None,
        };
        if let Some(n) = baru {
            *s = n;
        }
    }
}

/// Token panjang (huruf/angka/`+/_=-`) dengan entropi Shannon tinggi dan minimal dua kelas karakter:
/// ciri kunci/token acak. Hex murni (hash, commit) entropinya maksimal 4.0 sehingga lolos dengan ambang bawaan.
fn token_entropi_tinggi(teks: &str, min_panjang: usize, ambang: f64) -> Vec<(usize, usize)> {
    let b = teks.as_bytes();
    let mut hasil = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if !karakter_token(b[i]) {
            i += 1;
            continue;
        }
        let awal = i;
        while i < b.len() && karakter_token(b[i]) {
            i += 1;
        }
        let token = &b[awal..i];
        // '=' hanya dianggap padding base64 di ujung token; di tengah ia pemisah (mis. `KEY=nilai`).
        let mut akhir = i;
        while akhir < b.len() && b[akhir] == b'=' && akhir - i < 2 {
            akhir += 1;
        }
        i = akhir;
        if token.len() >= min_panjang && kelas_karakter(token) >= 2 && entropi_shannon(token) >= ambang {
            hasil.push((awal, akhir));
        }
    }
    hasil
}

fn karakter_token(c: u8) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, b'+' | b'/' | b'_' | b'-')
}

fn kelas_karakter(t: &[u8]) -> u8 {
    t.iter().any(u8::is_ascii_lowercase) as u8 + t.iter().any(u8::is_ascii_uppercase) as u8 + t.iter().any(u8::is_ascii_digit) as u8
}

pub fn entropi_shannon(t: &[u8]) -> f64 {
    let mut frek = [0u32; 256];
    for &c in t {
        frek[c as usize] += 1;
    }
    let n = t.len() as f64;
    frek.iter()
        .filter(|&&f| f > 0)
        .map(|&f| {
            let p = f as f64 / n;
            -p * p.log2()
        })
        .sum()
}
