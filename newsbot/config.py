"""Configuration: environment variables + YAML files, validated per run mode.

Every setting has a safe default. Names shared with bot v1 (WP_*, OPENAI_*, CAT_*,
MAX_POSTS_PER_RUN, FEED_ENTRIES_LIMIT, WP_TAGS_*, SEO_INTERNAL_LINKS_*) keep their old
meaning; everything new is prefixed NEWSBOT_. See docs/CONFIGURATION.md.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MODES = ("dry-run", "draft", "publish")
HARD_MAX_POSTS_PER_RUN = 1

REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Invalid or incomplete configuration. Never contains secret values."""


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value != "" else default


def env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    if raw == "":
        return default
    return raw.casefold() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = _env(name, "")
    try:
        value = int(raw) if raw != "" else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    raw = _env(name, "")
    try:
        value = float(raw) if raw != "" else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = _env(name, default).lower()
    if value not in choices:
        raise ConfigError(f"{name} must be one of {', '.join(choices)} (got {value!r})")
    return value


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = _env(name, "")
        if value:
            return value
    return default


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping at the top level")
    return data


@dataclass
class ModelPrice:
    input: float
    cached_input: float
    output: float
    source: str = ""


@dataclass
class Settings:
    mode: str = "dry-run"
    run_id: str = "local"
    repo_root: Path = REPO_ROOT

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-6-luna"
    openai_fallback_model: str = ""
    openai_api_style: str = "responses"
    reasoning_effort: str = "low"
    compose_reasoning_effort: str = "medium"
    openai_timeout: float = 90.0
    openai_max_retries: int = 3
    max_output_tokens_triage: int = 2500
    max_output_tokens_extract: int = 9000
    max_output_tokens_compose: int = 9000
    max_output_tokens_check: int = 4000
    pricing: dict[str, ModelPrice] = field(default_factory=dict)

    # Budget
    max_run_cost_usd: float = 0.10
    max_daily_cost_usd: float = 1.00
    max_llm_requests_per_run: int = 16
    max_candidates_per_run: int = 3
    max_posts_per_run: int = 1

    # WordPress
    wp_base_url: str = ""
    wp_username: str = ""
    wp_app_password: str = ""
    cat_all: int = 0
    cat_gaming: int = 0
    cat_hardware: int = 0
    cat_default: int = 0
    tags_enabled: bool = True
    tags_max: int = 2
    tag_create_policy: str = "covered"
    tag_create_min_posts: int = 2
    rankmath_enabled: bool = True
    wp_recent_posts: int = 40
    source_links_nofollow: bool = True

    # Feeds / HTTP
    sources_file: Path = REPO_ROOT / "sources.yaml"
    manual_links_file: Path = REPO_ROOT / "manual_links.txt"
    feed_entries_limit: int = 15
    max_story_age_hours: int = 36
    http_timeout: float = 20.0
    user_agent: str = "Mozilla/5.0 (compatible; PoormazNewsBot/2.0; +https://poormaz.com/)"
    respect_robots: bool = True

    # Research
    max_source_docs: int = 5
    max_doc_chars: int = 7000
    official_expansion: bool = True
    max_official_fetches: int = 2
    steam_context: bool = True

    # Editorial
    min_verified_claims: int = 3
    min_words: int = 130
    max_words: int = 900
    words_per_claim: int = 75
    rumor_policy: str = "corroborated"
    allow_single_source: bool = True
    single_source_min_authority: str = "high"
    triage_llm: bool = True
    llm_factcheck: bool = True
    max_revisions: int = 1
    cluster_window_hours: int = 72
    cluster_threshold: float = 0.55
    story_dedup_days: int = 10
    max_story_attempts: int = 3
    retry_backoff_minutes: int = 110

    # Links
    internal_links_enabled: bool = True
    max_internal_links: int = 3
    localization_file: Path = REPO_ROOT / "localization_pages.yaml"
    validate_links: bool = True

    # Images
    image_policy: str = "safe"
    fallback_featured_media_id: int = 0
    reuse_localization_image: bool = True

    # State / output
    state_db: Path = REPO_ROOT / "newsbot_state.db"
    legacy_db: Path = REPO_ROOT / "news_cache.db"
    legacy_sync: bool = True
    wp_recovery: bool = True
    report_dir: Path = REPO_ROOT / "artifacts"

    # YAML-driven configuration
    editorial: dict = field(default_factory=dict)
    entities_cfg: dict = field(default_factory=dict)
    official_domains: list[str] = field(default_factory=list)
    press_wire_domains: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def wp_configured(self) -> bool:
        return bool(self.wp_base_url and self.wp_username and self.wp_app_password)

    @property
    def secrets(self) -> list[str]:
        return [s for s in (self.openai_api_key, self.wp_app_password, _env("RANKMATH_UPDATER_TOKEN")) if s]

    def price_for(self, model: str) -> ModelPrice | None:
        if model in self.pricing:
            return self.pricing[model]
        # Dated snapshots ("gpt-6-luna-20260922") bill like their alias.
        for name in sorted(self.pricing, key=len, reverse=True):
            if model.startswith(name + "-"):
                return self.pricing[name]
        return None

    def validate(self, *, needs_llm: bool = True) -> list[str]:
        """Return human-readable problems. Secret values are never included."""
        problems: list[str] = []
        if self.mode not in MODES:
            problems.append(f"NEWSBOT_MODE must be one of {', '.join(MODES)}")
        if needs_llm and not self.openai_api_key:
            problems.append("OPENAI_API_KEY is missing (required for fact extraction and writing)")
        if self.mode in ("draft", "publish"):
            for name, value in (("WP_BASE_URL", self.wp_base_url), ("WP_USERNAME", self.wp_username),
                                ("WP_APP_PASSWORD", self.wp_app_password)):
                if not value:
                    problems.append(f"{name} is missing (required in {self.mode} mode)")
            if not (self.cat_all or self.cat_gaming or self.cat_default):
                problems.append("No WordPress category configured (CAT_ALL / CAT_GAMING / WP_CATEGORY_ID)")
        if self.wp_base_url and not self.wp_base_url.startswith("https://"):
            problems.append("WP_BASE_URL must start with https://")
        if needs_llm and self.openai_model and self.price_for(self.openai_model) is None:
            problems.append(f"No pricing configured for model {self.openai_model!r} in config/pricing.yaml "
                            "(budget limits need it)")
        if not self.sources_file.exists():
            problems.append(f"Sources file not found: {self.sources_file.name}")
        return problems


def load_pricing(path: Path) -> dict[str, ModelPrice]:
    data = load_yaml(path)
    models = data.get("models") or {}
    out: dict[str, ModelPrice] = {}
    for name, row in models.items():
        if not isinstance(row, dict):
            continue
        try:
            out[str(name)] = ModelPrice(
                input=float(row["input_per_1m"]),
                cached_input=float(row.get("cached_input_per_1m", row["input_per_1m"])),
                output=float(row["output_per_1m"]),
                source=str(row.get("source", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"config/pricing.yaml: invalid entry for {name}") from exc
    override = _env("NEWSBOT_PRICING_JSON")
    if override:
        try:
            for name, row in json.loads(override).items():
                out[name] = ModelPrice(float(row["input"]), float(row.get("cached_input", row["input"])),
                                       float(row["output"]), "NEWSBOT_PRICING_JSON")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ConfigError("NEWSBOT_PRICING_JSON is not valid JSON pricing") from exc
    return out


def load_settings(mode: str | None = None, repo_root: Path | None = None) -> Settings:
    root = Path(repo_root or REPO_ROOT)
    cfg_dir = root / "config"
    s = Settings(repo_root=root)

    s.mode = (mode or env_choice("NEWSBOT_MODE", "dry-run", MODES)).lower()
    s.run_id = _env("GITHUB_RUN_ID", "local") + "-" + _env("GITHUB_RUN_ATTEMPT", "1")

    s.openai_api_key = _env("OPENAI_API_KEY")
    s.openai_model = _env("OPENAI_MODEL", "gpt-6-luna")
    s.openai_fallback_model = _env("OPENAI_FALLBACK_MODEL")
    s.openai_api_style = env_choice("NEWSBOT_OPENAI_API_STYLE", "responses", ("responses", "chat"))
    efforts = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "off")
    s.reasoning_effort = env_choice("NEWSBOT_REASONING_EFFORT", "low", efforts)
    s.compose_reasoning_effort = env_choice("NEWSBOT_COMPOSE_REASONING_EFFORT", "medium", efforts)
    s.openai_timeout = env_float("NEWSBOT_OPENAI_TIMEOUT", 90.0, minimum=10.0)
    s.openai_max_retries = env_int("NEWSBOT_OPENAI_MAX_RETRIES", 3, 0, 6)
    s.max_output_tokens_extract = env_int("NEWSBOT_MAX_OUTPUT_TOKENS_EXTRACT", 9000, 1500, 32000)
    s.max_output_tokens_compose = env_int("NEWSBOT_MAX_OUTPUT_TOKENS_COMPOSE", 9000, 1500, 32000)
    s.max_output_tokens_check = env_int("NEWSBOT_MAX_OUTPUT_TOKENS_CHECK", 4000, 800, 16000)
    s.max_output_tokens_triage = env_int("NEWSBOT_MAX_OUTPUT_TOKENS_TRIAGE", 2500, 500, 8000)
    s.pricing = load_pricing(cfg_dir / "pricing.yaml")

    s.max_run_cost_usd = env_float("NEWSBOT_MAX_RUN_COST_USD", 0.10, minimum=0.0)
    s.max_daily_cost_usd = env_float("NEWSBOT_MAX_DAILY_COST_USD", 1.00, minimum=0.0)
    s.max_llm_requests_per_run = env_int("NEWSBOT_MAX_LLM_REQUESTS", 16, 0, 60)
    s.max_candidates_per_run = env_int("NEWSBOT_MAX_CANDIDATES", 3, 1, 10)
    # One post per run is a ceiling, never a quota.
    s.max_posts_per_run = env_int("MAX_POSTS_PER_RUN", 1, 0, HARD_MAX_POSTS_PER_RUN)

    s.wp_base_url = _env("WP_BASE_URL").rstrip("/")
    s.wp_username = _env("WP_USERNAME")
    s.wp_app_password = _env("WP_APP_PASSWORD")
    s.cat_all = env_int("CAT_ALL", 0, 0)
    s.cat_gaming = env_int("CAT_GAMING", 0, 0)
    s.cat_hardware = env_int("CAT_HARDWARE", 0, 0)
    s.cat_default = env_int("WP_CATEGORY_ID", 0, 0)
    s.tags_enabled = _first_env("WP_TAGS_ENABLED", "AUTO_TAGS_ENABLED", default="1") == "1"
    s.tags_max = max(0, min(3, int(_first_env("WP_TAGS_MAX", "AUTO_TAGS_MAX", default="2") or 2)))
    s.tag_create_policy = env_choice("NEWSBOT_TAG_CREATE_POLICY", "covered", ("never", "covered", "always"))
    s.tag_create_min_posts = env_int("NEWSBOT_TAG_CREATE_MIN_POSTS", 2, 1, 20)
    s.rankmath_enabled = env_bool("NEWSBOT_RANKMATH", True)
    s.wp_recent_posts = env_int("NEWSBOT_WP_RECENT_POSTS", 40, 10, 100)
    s.source_links_nofollow = env_bool("NEWSBOT_SOURCE_LINKS_NOFOLLOW", True)

    s.feed_entries_limit = env_int("FEED_ENTRIES_LIMIT", 15, 1, 60)
    s.max_story_age_hours = env_int("NEWSBOT_MAX_STORY_AGE_HOURS", 36, 6, 240)
    s.http_timeout = env_float("HTTP_TIMEOUT", 20.0, minimum=5.0)
    s.user_agent = _env("NEWSBOT_USER_AGENT", s.user_agent)
    s.respect_robots = env_bool("NEWSBOT_RESPECT_ROBOTS", True)

    s.max_source_docs = env_int("NEWSBOT_MAX_SOURCE_DOCS", 5, 1, 8)
    s.max_doc_chars = env_int("NEWSBOT_MAX_DOC_CHARS", 7000, 1500, 20000)
    s.official_expansion = env_bool("NEWSBOT_OFFICIAL_EXPANSION", True)
    s.max_official_fetches = env_int("NEWSBOT_MAX_OFFICIAL_FETCHES", 2, 0, 5)
    s.steam_context = env_bool("NEWSBOT_STEAM_CONTEXT", True)

    s.min_verified_claims = env_int("NEWSBOT_MIN_VERIFIED_CLAIMS", 3, 1, 10)
    s.min_words = env_int("NEWSBOT_MIN_WORDS", 130, 60, 600)
    s.max_words = env_int("NEWSBOT_MAX_WORDS", 900, 200, 2000)
    s.words_per_claim = env_int("NEWSBOT_WORDS_PER_CLAIM", 75, 30, 200)
    s.rumor_policy = env_choice("NEWSBOT_RUMOR_POLICY", "corroborated", ("reject", "corroborated"))
    s.allow_single_source = env_bool("NEWSBOT_ALLOW_SINGLE_SOURCE", True)
    s.single_source_min_authority = env_choice("NEWSBOT_SINGLE_SOURCE_MIN_AUTHORITY", "high",
                                               ("high", "medium", "low"))
    s.triage_llm = env_bool("NEWSBOT_TRIAGE_LLM", True)
    s.llm_factcheck = env_bool("NEWSBOT_LLM_FACTCHECK", True)
    s.max_revisions = env_int("NEWSBOT_MAX_REVISIONS", 1, 0, 2)
    s.cluster_window_hours = env_int("NEWSBOT_CLUSTER_WINDOW_HOURS", 72, 12, 240)
    s.cluster_threshold = env_float("NEWSBOT_CLUSTER_THRESHOLD", 0.55, minimum=0.2)
    s.story_dedup_days = env_int("NEWSBOT_STORY_DEDUP_DAYS", 10, 1, 60)
    s.max_story_attempts = env_int("NEWSBOT_MAX_STORY_ATTEMPTS", 3, 1, 10)
    s.retry_backoff_minutes = env_int("NEWSBOT_RETRY_BACKOFF_MINUTES", 110, 10, 1440)

    s.internal_links_enabled = (_first_env("SEO_INTERNAL_LINKS_ENABLED", default="1") == "1")
    s.max_internal_links = max(0, min(4, int(_first_env("SEO_INTERNAL_LINKS_MAX", default="3") or 3)))
    s.localization_file = root / _env("LOCALIZATION_PAGES_FILE", "localization_pages.yaml")
    s.validate_links = env_bool("NEWSBOT_VALIDATE_LINKS", True)

    s.image_policy = env_choice("NEWSBOT_IMAGE_POLICY", "safe", ("none", "safe", "legacy"))
    s.fallback_featured_media_id = env_int("NEWSBOT_FALLBACK_MEDIA_ID", 0, 0)
    s.reuse_localization_image = env_bool("NEWSBOT_REUSE_LOCALIZATION_IMAGE", True)

    s.sources_file = root / _env("SOURCES_FILE", "sources.yaml")
    s.manual_links_file = root / _env("MANUAL_LINKS_FILE", "manual_links.txt")
    s.state_db = root / _env("NEWSBOT_STATE_DB", "newsbot_state.db")
    s.legacy_db = root / _env("DB_FILE", "news_cache.db")
    s.legacy_sync = env_bool("NEWSBOT_LEGACY_SYNC", True)
    s.wp_recovery = env_bool("NEWSBOT_WP_RECOVERY", True)
    s.report_dir = root / _env("NEWSBOT_REPORT_DIR", "artifacts")

    s.editorial = load_yaml(cfg_dir / "editorial.yaml")
    s.entities_cfg = load_yaml(cfg_dir / "entities.yaml")
    domains = load_yaml(cfg_dir / "official_domains.yaml")
    s.official_domains = [str(d).lower() for d in (domains.get("official") or [])]
    s.press_wire_domains = [str(d).lower() for d in (domains.get("press_wires") or [])]
    return s
