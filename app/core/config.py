"""Server-only configuration. No payment-provider credentials live here."""

from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "true" if default else "false")
    return value.strip().lower() == "true"


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Reportlume")
    app_env: str = os.getenv("APP_ENV", "development")
    base_url: str = os.getenv("BASE_URL", "http://127.0.0.1:8000")
    contact_email: str = os.getenv("CONTACT_EMAIL", "")

    # Seller identity shown on the public legal pages. Production refuses
    # customer data until the name, country and contact e-mail are set.
    seller_name: str = os.getenv("SELLER_NAME", "")
    seller_country: str = os.getenv("SELLER_COUNTRY", "")
    seller_address: str = os.getenv("SELLER_ADDRESS", "")

    firebase_project_id: str = os.getenv("FIREBASE_PROJECT_ID", "")
    firebase_web_api_key: str = os.getenv("FIREBASE_WEB_API_KEY", "")
    firebase_service_account_json: str = os.getenv(
        "FIREBASE_SERVICE_ACCOUNT_JSON", ""
    )
    google_credentials_path: str = os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS", ""
    )
    import_preview_secret: str = os.getenv("IMPORT_PREVIEW_SECRET", "")

    # Legacy capacity fields retained for workspace compatibility.
    professional_active_client_limit: int = 200
    trial_hours: int = 72
    trial_client_limit: int = 1
    trial_logical_report_limit: int = 1
    session_days: int = 5

    data_deletion_enabled: bool = _flag("DATA_DELETION_ENABLED", False)
    legal_review_complete: bool = _flag("LEGAL_REVIEW_COMPLETE", False)
    privacy_review_complete: bool = _flag("PRIVACY_REVIEW_COMPLETE", False)
    security_qa_complete: bool = _flag("SECURITY_QA_COMPLETE", False)
    data_lifecycle_qa_complete: bool = _flag(
        "DATA_LIFECYCLE_QA_COMPLETE", False
    )
    railway_single_instance_verified: bool = _flag(
        "RAILWAY_SINGLE_INSTANCE_VERIFIED", False
    )
    railway_client_ip_verified: bool = _flag(
        "RAILWAY_CLIENT_IP_VERIFIED", False
    )
    trust_railway_real_ip: bool = _flag("TRUST_RAILWAY_REAL_IP", False)
    auth_rate_limit_key_secret: str = os.getenv(
        "AUTH_RATE_LIMIT_KEY_SECRET", ""
    )
    # Local Firestore emulator ("host:port"); refused in production.
    firestore_emulator_host: str = os.getenv("FIRESTORE_EMULATOR_HOST", "")
    ip_diagnostics_enabled: bool = _flag("IP_DIAGNOSTICS_ENABLED", False)
    ip_diagnostics_secret: str = os.getenv("IP_DIAGNOSTICS_SECRET", "")

    @property
    def production(self) -> bool:
        # Unknown deployment environments must not silently disable
        # production safeguards. Only explicit local/test modes are nonproduction.
        return self.app_env.strip().lower() not in {"development", "test"}

    @property
    def firebase_ready(self) -> bool:
        return bool(
            self.firebase_project_id
            and self.firebase_web_api_key
            and (
                self.firebase_service_account_json
                or self.google_credentials_path
            )
        )

    @property
    def session_cookie_name(self) -> str:
        return (
            "__Host-reportlume_session" if self.production
            else "reportlume_session"
        )

    @property
    def csrf_cookie_name(self) -> str:
        return (
            "__Host-reportlume_csrf" if self.production
            else "reportlume_csrf"
        )


settings = Settings()
