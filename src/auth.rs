use axum::{
    extract::{Request, State},
    http::{StatusCode, header},
    middleware::Next,
    response::{IntoResponse, Response},
};

use std::sync::Arc;

use crate::{error::ApiError, keys::KeyInfo, proxy::AppState};

/// Identitas pemanggil yang sudah lolos autentikasi; disisipkan ke request untuk handler berikutnya.
#[derive(Debug, Clone)]
pub struct Identitas {
    pub key_id: i64,
    pub nama: String,
    /// Batas efektif: batas key sendiri, atau default dari config.
    pub rpm: Option<u64>,
    pub tpm: Option<u64>,
    /// Data key dari cache (None bila auth dimatikan). Dipakai `/v1/key/info`.
    pub info: Option<Arc<KeyInfo>>,
}

impl Identitas {
    fn anonim() -> Self {
        Self { key_id: 0, nama: "anonim".into(), rpm: None, tpm: None, info: None }
    }
}

fn galat_401(code: &'static str, pesan: &str) -> Response {
    let mut r = ApiError::new(StatusCode::UNAUTHORIZED, "invalid_request_error", code, pesan).into_response();
    r.headers_mut().insert(header::WWW_AUTHENTICATE, header::HeaderValue::from_static("Bearer"));
    r
}

/// Middleware untuk /v1/*: memeriksa `Authorization: Bearer <key>` terhadap virtual key aktif.
pub async fn autentikasi(State(s): State<AppState>, mut req: Request, next: Next) -> Response {
    let rt = s.runtime();
    if !rt.config.auth_required {
        req.extensions_mut().insert(Identitas::anonim());
        return next.run(req).await;
    }

    let token = req
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.split_once(' '))
        .filter(|(skema, _)| skema.eq_ignore_ascii_case("bearer"))
        .map(|(_, t)| t.trim().to_string());

    let Some(token) = token.filter(|t| !t.is_empty()) else {
        return galat_401("missing_api_key", "API key wajib disertakan: header 'Authorization: Bearer <key>'.");
    };
    // Pesan sama untuk key salah/dicabut: tidak membocorkan apakah key pernah ada.
    let Some(info) = s.keys.authenticate(&token) else {
        return galat_401("invalid_api_key", "API key tidak valid.");
    };

    let (rpm, tpm) = (info.rpm.or(rt.config.default_rpm), info.tpm.or(rt.config.default_tpm));
    req.extensions_mut().insert(Identitas { key_id: info.id, nama: info.name.clone(), rpm, tpm, info: Some(info) });
    next.run(req).await
}
