"""Settings, all from environment variables (see .env.example)."""
import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip().rstrip("/") if isinstance(value, str) else value


@dataclass(frozen=True)
class Settings:
    # Jellyfin: every request is authorized by asking Jellyfin who the caller's token belongs to.
    jellyfin_url: str = field(default_factory=lambda: _env("JELLYFIN_URL", "http://jellyfin:8096"))

    # Dispatcharr: a service account reads channels and the guide. `public_url` is what TVs use to play streams.
    dispatcharr_url: str | None = field(default_factory=lambda: _env("DISPATCHARR_URL"))
    dispatcharr_public_url: str | None = field(default_factory=lambda: _env("DISPATCHARR_PUBLIC_URL") or _env("DISPATCHARR_URL"))
    dispatcharr_username: str | None = field(default_factory=lambda: _env("DISPATCHARR_USERNAME"))
    dispatcharr_password: str | None = field(default_factory=lambda: os.environ.get("DISPATCHARR_PASSWORD"))
    # Channel upkeep after each refresh (app/lineup.py): backup streams from the other IPTV accounts, duplicates
    # hidden, pinned channel numbers kept in order. This changes channels, so the Dispatcharr user needs the Admin
    # level. DISPATCHARR_BACKUP_STREAMS is the older name.
    dispatcharr_manage_channels: bool = field(
        default_factory=lambda: (_env("DISPATCHARR_MANAGE_CHANNELS") or _env("DISPATCHARR_BACKUP_STREAMS") or "").lower()
        in ("1", "true", "yes"))

    # Calendar sources (optional; each one that's configured is merged in).
    sonarr_url: str | None = field(default_factory=lambda: _env("SONARR_URL"))
    sonarr_api_key: str | None = field(default_factory=lambda: _env("SONARR_API_KEY"))
    radarr_url: str | None = field(default_factory=lambda: _env("RADARR_URL"))
    radarr_api_key: str | None = field(default_factory=lambda: _env("RADARR_API_KEY"))

    # Requests (optional): Jellyseerr / Seerr, acting as each signed-in Jellyfin user with the admin API key.
    seerr_url: str | None = field(default_factory=lambda: _env("SEERR_URL") or _env("JELLYSEERR_URL"))
    seerr_api_key: str | None = field(default_factory=lambda: _env("SEERR_API_KEY") or _env("JELLYSEERR_API_KEY"))

    # Where listings without an explicit zone (e.g. "7pm Devils at Rangers") are assumed to be.
    local_timezone: str = field(default_factory=lambda: _env("LOCAL_TIMEZONE", "America/New_York"))

    # Optional JSON file overriding the built-in category rules (see app/categories.py).
    categories_file: str | None = field(default_factory=lambda: _env("CATEGORIES_FILE"))

    channel_refresh_seconds: int = field(default_factory=lambda: int(_env("CHANNEL_REFRESH_SECONDS", "120")))
    guide_refresh_seconds: int = field(default_factory=lambda: int(_env("GUIDE_REFRESH_SECONDS", "300")))
    guide_hours_ahead: int = field(default_factory=lambda: int(_env("GUIDE_HOURS_AHEAD", "24")))

    @property
    def livetv_enabled(self) -> bool:
        return bool(self.dispatcharr_url and self.dispatcharr_username and self.dispatcharr_password)

    @property
    def requests_enabled(self) -> bool:
        return bool(self.seerr_url and self.seerr_api_key)

    @property
    def calendar_enabled(self) -> bool:
        return bool((self.sonarr_url and self.sonarr_api_key) or (self.radarr_url and self.radarr_api_key))


settings = Settings()
