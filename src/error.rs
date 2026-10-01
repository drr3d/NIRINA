use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde_json::json;

/// Galat ke klien dalam format error OpenAI: {"error": {"message", "type", "code"}}.
#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub kind: &'static str,
    pub code: &'static str,
    pub message: String,
    /// Diisi untuk 429: dikirim sebagai header Retry-After (detik).
    pub retry_after: Option<u64>,
}

impl ApiError {
    pub fn new(status: StatusCode, kind: &'static str, code: &'static str, message: impl Into<String>) -> Self {
        Self { status, kind, code, message: message.into(), retry_after: None }
    }

    /// `respons` = true bila yang ditahan adalah jawaban dari upstream (bukan request klien).
    pub fn guardrail_diblok(aturan: &[String], respons: bool) -> Self {
        let daftar = aturan.join(", ");
        if respons {
            Self::new(
                StatusCode::BAD_GATEWAY,
                "api_error",
                "guardrail_blocked",
                format!("Respons upstream ditahan guardrail (terdeteksi: {daftar})."),
            )
        } else {
            Self::new(
                StatusCode::FORBIDDEN,
                "invalid_request_error",
                "guardrail_blocked",
                format!("Request ditolak guardrail: terdeteksi data sensitif ({daftar}). Hapus data tersebut lalu coba lagi."),
            )
        }
    }

    pub fn rate_limited(tolak: crate::limiter::Tolak) -> Self {
        let mut e = Self::new(
            StatusCode::TOO_MANY_REQUESTS,
            "rate_limit_error",
            "rate_limit_exceeded",
            format!("Batas {} untuk API key ini terlampaui. Coba lagi dalam {} detik.", tolak.nama(), tolak.retry_after_detik()),
        );
        e.retry_after = Some(tolak.retry_after_detik());
        e
    }

    pub fn bad_request(code: &'static str, message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, "invalid_request_error", code, message)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = json!({ "error": { "message": self.message, "type": self.kind, "code": self.code } });
        let mut resp = (self.status, Json(body)).into_response();
        resp.extensions_mut().insert(crate::stats::KodeGalat(self.code));
        if let Some(d) = self.retry_after {
            resp.headers_mut().insert(axum::http::header::RETRY_AFTER, axum::http::HeaderValue::from(d));
        }
        resp
    }
}
