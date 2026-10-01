use std::{
    collections::HashMap,
    sync::Mutex,
    time::{Duration, Instant},
};

use crate::util::kunci;

#[derive(Default, Clone, Copy)]
struct Keadaan {
    gagal_beruntun: u32,
    cooldown_hingga: Option<Instant>,
}

/// Ringkasan keadaan satu upstream (untuk log dan nanti API admin).
#[derive(Debug, Clone, PartialEq)]
pub struct Status {
    pub gagal_beruntun: u32,
    /// Sisa waktu cooldown; None bila upstream sedang dianggap sehat.
    pub sisa_cooldown: Option<Duration>,
}

/// Pelacak kesehatan upstream di memori, dikunci dengan `Upstream::id`.
///
/// Upstream yang gagal masuk cooldown dan dilewati selama waktu itu, kecuali tidak ada pilihan lain: cooldown
/// hanya mengubah URUTAN percobaan (yang sehat dulu), tidak pernah membuat request gagal tanpa mencoba apa pun.
#[derive(Default)]
pub struct Kesehatan {
    peta: Mutex<HashMap<String, Keadaan>>,
}

impl Kesehatan {
    /// Urutan percobaan: upstream sehat sesuai urutan config, lalu yang sedang cooldown (paling cepat pulih dulu).
    /// Elemen kedua true = sedang cooldown (hanya boleh dicoba sekali, tanpa retry).
    pub fn urutan(&self, ids: &[&str], now: Instant) -> Vec<(usize, bool)> {
        let peta = kunci(&self.peta);
        let mut sehat = Vec::new();
        let mut dingin = Vec::new();
        for (i, id) in ids.iter().enumerate() {
            match peta.get(*id).and_then(|k| k.cooldown_hingga).filter(|&h| h > now) {
                Some(hingga) => dingin.push((i, hingga)),
                None => sehat.push((i, false)),
            }
        }
        dingin.sort_by_key(|&(_, h)| h);
        sehat.extend(dingin.into_iter().map(|(i, _)| (i, true)));
        sehat
    }

    pub fn sukses(&self, id: &str) {
        kunci(&self.peta).remove(id);
    }

    pub fn gagal(&self, id: &str, cooldown: Duration, now: Instant) {
        let mut peta = kunci(&self.peta);
        let k = peta.entry(id.to_string()).or_default();
        k.gagal_beruntun += 1;
        k.cooldown_hingga = Some(now + cooldown);
    }

    pub fn status(&self, id: &str, now: Instant) -> Status {
        let k = kunci(&self.peta).get(id).copied().unwrap_or_default();
        Status {
            gagal_beruntun: k.gagal_beruntun,
            sisa_cooldown: k.cooldown_hingga.and_then(|h| h.checked_duration_since(now)).filter(|d| !d.is_zero()),
        }
    }
}
