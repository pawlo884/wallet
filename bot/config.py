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

    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_allowed: set[int] = field(default_factory=lambda: _ids("TELEGRAM_ALLOWED_USERS"))

    discord_token: str = field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", ""))
    discord_allowed: set[int] = field(default_factory=lambda: _ids("DISCORD_ALLOWED_USERS"))
    # Kanały serwera, na których bot reaguje (poza DM). Puste = tylko DM.
    discord_channels: set[int] = field(default_factory=lambda: _ids("DISCORD_CHANNEL_IDS"))
