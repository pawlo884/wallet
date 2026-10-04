import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "tak")


@dataclass(frozen=True)
class Config:
    wallet_token: str = field(default_factory=lambda: os.environ["WALLET_API_TOKEN"])
    wallet_base_url: str = field(
        default_factory=lambda: os.getenv("WALLET_API_URL", "https://rest.budgetbakers.com/wallet")
    )
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"))
    # Nazwa lub ID konta używanego, gdy wiadomość nie wskazuje konta.
    default_account: str = field(default_factory=lambda: os.getenv("DEFAULT_ACCOUNT", ""))
    base_currency: str = field(default_factory=lambda: os.getenv("BASE_CURRENCY", "PLN"))
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(os.getenv("TZ", "Europe/Warsaw")))
    require_confirmation: bool = field(default_factory=lambda: _bool("REQUIRE_CONFIRMATION", True))

    schedule_file: str = field(default_factory=lambda: os.getenv("SCHEDULE_FILE", "config/schedule.yaml"))
    state_file: str = field(default_factory=lambda: os.getenv("STATE_FILE", "data/state.json"))
    # Od której godziny (czas lokalny) wysyłać przypomnienia o płatnościach.
    reminder_hour: int = field(default_factory=lambda: int(os.getenv("REMINDER_HOUR", "9")))

    # Mowa → tekst (lokalnie, faster-whisper). Modele: tiny/base/small/medium — większy = lepiej i wolniej.
    stt_enabled: bool = field(default_factory=lambda: _bool("STT_ENABLED", True))
    stt_model: str = field(default_factory=lambda: os.getenv("STT_MODEL", "small"))
    stt_threads: int = field(default_factory=lambda: int(os.getenv("STT_THREADS", "4")))

    # Prognoza: założenia w pliku, strona WWW na porcie (0 = wyłączona), publiczny adres do linku w /prognoza.
    forecast_file: str = field(default_factory=lambda: os.getenv("FORECAST_FILE", "config/forecast.yaml"))
    web_port: int = field(default_factory=lambda: int(os.getenv("WEB_PORT", "8080")))
    forecast_url: str = field(default_factory=lambda: os.getenv("FORECAST_URL", ""))

    # Skrzynka bota na wyciągi (IMAP). Puste MAIL_USER = wyłączone.
    mail_imap_host: str = field(default_factory=lambda: os.getenv("MAIL_IMAP_HOST", "imap.gmail.com"))
    mail_user: str = field(default_factory=lambda: os.getenv("MAIL_USER", ""))
    mail_password: str = field(default_factory=lambda: os.getenv("MAIL_PASSWORD", ""))
    mail_folder: str = field(default_factory=lambda: os.getenv("MAIL_FOLDER", "INBOX"))
    mail_poll_minutes: int = field(default_factory=lambda: int(os.getenv("MAIL_POLL_MINUTES", "10")))
    # Adresy/domeny, z których maile są przetwarzane (Twój adres i domena banku), np. "ja@gmail.com,mbank.pl"
    mail_allowed_from: set[str] = field(
        default_factory=lambda: {x.strip().lower() for x in os.getenv("MAIL_ALLOWED_FROM", "").split(",") if x.strip()}
    )

    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_allowed: set[int] = field(default_factory=lambda: _ids("TELEGRAM_ALLOWED_USERS"))
