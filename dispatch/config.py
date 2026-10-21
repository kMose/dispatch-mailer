"""Configuration models and loader.

All paths in campaign.yaml are resolved relative to the config file's directory, so a
campaign folder can be moved or zipped without breaking.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator


class ConfigError(ValueError):
    pass


TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_ALIASES = {
    "mon": "Mon", "monday": "Mon",
    "tue": "Tue", "tues": "Tue", "tuesday": "Tue",
    "wed": "Wed", "weds": "Wed", "wednesday": "Wed",
    "thu": "Thu", "thur": "Thu", "thurs": "Thu", "thursday": "Thu",
    "fri": "Fri", "friday": "Fri",
    "sat": "Sat", "saturday": "Sat",
    "sun": "Sun", "sunday": "Sun",
}


def parse_hhmm(value: str) -> time:
    if not isinstance(value, str) or not TIME_RE.match(value.strip()):
        raise ValueError(f"expected HH:MM, got {value!r}")
    h, m = value.strip().split(":")
    return time(int(h), int(m))


class SMTPConfig(BaseModel):
    host: str = "smtp.example.com"
    port: int = 587
    starttls: bool = True
    timeout: int = 30
    ssl: bool = False


class IMAPConfig(BaseModel):
    host: str = "imap.example.com"
    port: int = 993
    ssl: bool = True
    mailbox: str = "INBOX"


class SenderConfig(BaseModel):
    id: str = "sam"
    from_name: str = "Sam at Samlax"
    from_email: str = "sam@samlax.net"
    reply_to: str | None = "sam@samlax.net"
    smtp: SMTPConfig = Field(default_factory=SMTPConfig)
    imap: IMAPConfig = Field(default_factory=IMAPConfig)
    secret_ref: str = "keyring:dispatch/sam"
    max_per_connection: int = 50


class SendWindow(BaseModel):
    enabled: bool = True
    start: str = "09:00"
    end: str = "18:00"
    days: list[str] = Field(default_factory=lambda: ["Mon", "Tue", "Wed", "Thu", "Fri"])
    timezone: str = "Europe/London"

    @field_validator("start", "end")
    @classmethod
    def _check_time(cls, v: str) -> str:
        parse_hhmm(v)
        return v.strip()

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @field_validator("days")
    @classmethod
    def _check_days(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for d in v:
            key = str(d).strip()[:3].lower()
            if key not in _DAY_ALIASES:
                raise ValueError(f"unknown weekday {d!r}")
            canon = _DAY_ALIASES[key]
            if canon not in out:
                out.append(canon)
        if not out:
            raise ValueError("send window needs at least one day")
        return out

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - depends on tzdata
            raise ConfigError(f"unknown timezone {self.timezone!r}") from exc

    def is_open(self, local_dt: datetime) -> bool:
        if not self.enabled:
            return True
        if DAY_NAMES[local_dt.weekday()] not in self.days:
            return False
        t = local_dt.time()
        return parse_hhmm(self.start) <= t < parse_hhmm(self.end)

    def seconds_left(self, local_dt: datetime) -> int:
        if not self.enabled:
            return 24 * 3600
        if not self.is_open(local_dt):
            return 0
        end = datetime.combine(local_dt.date(), parse_hhmm(self.end), tzinfo=local_dt.tzinfo)
        return max(0, int((end - local_dt).total_seconds()))

    def next_open(self, local_dt: datetime) -> datetime:
        if not self.enabled or self.is_open(local_dt):
            return local_dt
        for offset in range(0, 8):
            day = local_dt.date() + timedelta(days=offset)
            if DAY_NAMES[day.weekday()] not in self.days:
                continue
            candidate = datetime.combine(day, parse_hhmm(self.start), tzinfo=local_dt.tzinfo)
            if candidate > local_dt:
                return candidate
        # No window in the next week: treat as permanently closed.
        return local_dt + timedelta(days=365)


class CampaignConfig(BaseModel):
    name: str = "Untitled campaign"
    sheet_path: str = "./leads.xlsx"
    sheet_tab: str | None = None
    date_order: Literal["DMY", "MDY", "ISO", "auto"] = "DMY"
    quota: int = Field(default=200, ge=1)
    frequency_days: int = Field(default=30, ge=1)
    shuffle_order: bool = True
    send_window: SendWindow = Field(default_factory=SendWindow)


class ThrottleConfig(BaseModel):
    mode: Literal["fixed", "random"] = "random"
    fixed_seconds: int = Field(default=120, ge=0)
    random_seconds: list[int] = Field(default_factory=lambda: [60, 260])
    backoff_seconds: list[int] = Field(default_factory=lambda: [300, 900, 1800, 3600])

    @model_validator(mode="after")
    def _check_ranges(self) -> "ThrottleConfig":
        if self.mode == "random":
            if len(self.random_seconds) != 2:
                raise ValueError("random_seconds must be [min, max]")
            lo, hi = self.random_seconds
            if lo < 0 or hi < lo:
                raise ValueError("random_seconds must satisfy 0 <= min <= max")
        if not self.backoff_seconds:
            raise ValueError("backoff_seconds must not be empty")
        return self

    def mean_seconds(self) -> float:
        if self.mode == "fixed":
            return float(self.fixed_seconds)
        lo, hi = self.random_seconds
        return (lo + hi) / 2.0


class LimitsConfig(BaseModel):
    daily_cap: int = Field(default=200, ge=1)
    hourly_cap: int = Field(default=30, ge=1)


class SafetyConfig(BaseModel):
    dry_run: bool = True
    require_typed_confirmation_over: int = 50
    stop_on_failure_rate: float = Field(default=0.10, ge=0.0, le=1.0)


class WarmupConfig(BaseModel):
    enabled: bool = False
    ramp: list[int] = Field(default_factory=lambda: [30, 60, 100, 150, 200])
    first_send_date: date | None = None


class FooterConfig(BaseModel):
    postal_address: str = ""
    unsubscribe_url: str = ""
    unsubscribe_mailto: str = "unsubscribe@samlax.net"


class TemplateConfig(BaseModel):
    kind: Literal["blocks", "raw"] | None = None
    subject: str = "Quick question"
    html_path: str = "./templates/promo.html"
    text_path: str = "./templates/promo.txt"
    blocks_path: str | None = None
    image_url: str | None = None
    footer: FooterConfig = Field(default_factory=FooterConfig)

    @field_validator("html_path", "text_path", "blocks_path")
    @classmethod
    def _no_path_escape(cls, v: str | None) -> str | None:
        if v and ".." in Path(v).parts:
            raise ValueError("template paths may not contain '..'")
        return v

    @field_validator("image_url")
    @classmethod
    def _https_image(cls, v: str | None) -> str | None:
        if v and not v.startswith("https://"):
            raise ValueError("image_url must start with https://")
        return v


class FullConfig(BaseModel):
    campaign: CampaignConfig = Field(default_factory=CampaignConfig)
    throttle: ThrottleConfig = Field(default_factory=ThrottleConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    warmup: WarmupConfig = Field(default_factory=WarmupConfig)
    sender: SenderConfig = Field(default_factory=SenderConfig)
    template: TemplateConfig = Field(default_factory=TemplateConfig)

    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)
    _path: Path | None = PrivateAttr(default=None)

    # -- path handling -------------------------------------------------
    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def config_path(self) -> Path | None:
        return self._path

    def resolve(self, value: str | None) -> Path | None:
        if not value:
            return None
        p = Path(os.path.expandvars(os.path.expanduser(value)))
        return p if p.is_absolute() else (self._base_dir / p).resolve()

    @property
    def sheet_file(self) -> Path:
        return self.resolve(self.campaign.sheet_path) or Path("leads.xlsx")

    @property
    def html_file(self) -> Path:
        return self.resolve(self.template.html_path) or Path("promo.html")

    @property
    def text_file(self) -> Path:
        return self.resolve(self.template.text_path) or Path("promo.txt")

    @property
    def blocks_file(self) -> Path:
        p = self.resolve(self.template.blocks_path)
        if p:
            return p
        return self.html_file.with_suffix(".json")

    @property
    def outbox_dir(self) -> Path:
        return self._base_dir / "outbox"

    @property
    def db_path(self) -> Path:
        return self._base_dir / "dispatch.sqlite3"

    def template_kind(self) -> Literal["blocks", "raw"]:
        if self.template.kind:
            return self.template.kind
        return "blocks" if self.blocks_file.exists() else "raw"

    # -- behaviour -----------------------------------------------------
    def effective_warmup_cap(self, first_send: date | None, today: date, daily_cap: int) -> int:
        if not self.warmup.enabled or not first_send or not self.warmup.ramp:
            return daily_cap
        weeks = max(0, (today - first_send).days // 7)
        ramp = self.warmup.ramp
        ceiling = ramp[min(weeks, len(ramp) - 1)]
        return min(daily_cap, ceiling)

    def window_now(self) -> datetime:
        return datetime.now(self.campaign.send_window.tz)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_config(path: str | Path) -> FullConfig:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ConfigError(f"config not found: {p}")
    data = _read_yaml(p)
    try:
        cfg = FullConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"{p}: {exc}") from exc
    cfg._base_dir = p.parent
    cfg._path = p
    return cfg


def dump_config(cfg: FullConfig) -> str:
    data = cfg.model_dump(mode="json", exclude_none=False)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def write_config(cfg: FullConfig, path: str | Path) -> Path:
    p = Path(path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_config(cfg), encoding="utf-8")
    cfg._base_dir = p.parent
    cfg._path = p
    return p


DEFAULT_YAML = """\
campaign:
  name: "October promo"
  sheet_path: "./leads.xlsx"
  sheet_tab: null
  date_order: "DMY"
  quota: 200
  frequency_days: 30
  shuffle_order: true
  send_window:
    enabled: true
    start: "09:00"
    end: "18:00"
    days: [Mon, Tue, Wed, Thu, Fri]
    timezone: "Europe/London"

throttle:
  mode: "random"
  fixed_seconds: 120
  random_seconds: [60, 260]
  backoff_seconds: [300, 900, 1800, 3600]

limits:
  daily_cap: 200
  hourly_cap: 30

safety:
  dry_run: true
  require_typed_confirmation_over: 50
  stop_on_failure_rate: 0.10

warmup:
  enabled: true
  ramp: [30, 60, 100, 150, 200]
  first_send_date: null

sender:
  id: "sam"
  from_name: "Sam at Samlax"
  from_email: "sam@samlax.net"
  reply_to: "sam@samlax.net"
  smtp: { host: "smtp.PROVIDER", port: 587, starttls: true, timeout: 30 }
  imap: { host: "imap.PROVIDER", port: 993, ssl: true, mailbox: "INBOX" }
  secret_ref: "keyring:dispatch/sam"

template:
  kind: "blocks"
  subject: "Quick question, {FirstName|default:\\"there\\"}"
  html_path: "./templates/promo.html"
  text_path: "./templates/promo.txt"
  blocks_path: "./templates/promo.json"
  image_url: ""
  footer:
    postal_address: "Samlax, 1 Example Street, London, E1 6AN"
    unsubscribe_url: "https://samlax.net/u/{token}"
    unsubscribe_mailto: "unsubscribe@samlax.net"
"""


def default_config(base_dir: str | Path | None = None) -> FullConfig:
    data = yaml.safe_load(DEFAULT_YAML)
    cfg = FullConfig.model_validate(data)
    cfg._base_dir = Path(base_dir).resolve() if base_dir else Path.cwd()
    return cfg
