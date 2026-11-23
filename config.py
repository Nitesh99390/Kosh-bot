import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    dump_channel_id: int
    admin_ids: tuple[int, ...]
    db_path: str
    inline_page_size: int
    link_code_ttl_seconds: int
    force_sub_channel: str = ""
    force_sub_url: str = ""
    rate_limit_interval: float = 0.35


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN is missing in .env")

    raw_dump_id = os.getenv("DUMP_CHANNEL_ID", "").strip()
    if not raw_dump_id:
        raise ValueError("DUMP_CHANNEL_ID is missing in .env")
    try:
        dump_channel_id = int(raw_dump_id)
    except ValueError as exc:
        raise ValueError("DUMP_CHANNEL_ID must be an integer (e.g. -1001234567890)") from exc

    raw_admins = os.getenv("ADMIN_IDS", "").strip()
    admin_ids: list[int] = []
    if raw_admins:
        for item in raw_admins.split(","):
            item = item.strip()
            if item:
                admin_ids.append(int(item))

    db_path = os.getenv("DB_PATH", "data/koshbox.db").strip() or "data/koshbox.db"
    inline_page_size = min(max(int(os.getenv("INLINE_PAGE_SIZE", "25")), 5), 50)
    link_code_ttl = max(int(os.getenv("LINK_CODE_TTL_SECONDS", "300")), 60)
    force_sub_channel = os.getenv("FORCE_SUB_CHANNEL", "").strip()
    force_sub_url = os.getenv("FORCE_SUB_URL", "").strip()
    rate_limit_interval = max(float(os.getenv("RATE_LIMIT_INTERVAL", "0.35")), 0.0)

    return Config(
        bot_token=bot_token,
        dump_channel_id=dump_channel_id,
        admin_ids=tuple(admin_ids),
        db_path=db_path,
        inline_page_size=inline_page_size,
        link_code_ttl_seconds=link_code_ttl,
        force_sub_channel=force_sub_channel,
        force_sub_url=force_sub_url,
        rate_limit_interval=rate_limit_interval,
    )
