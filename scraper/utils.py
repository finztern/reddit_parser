from datetime import datetime, timezone


# ---------------------------------------------------------------- #
#  Вспомогательные функции
# ---------------------------------------------------------------- #

def iso_utc(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


def is_nsfw(comment_data: dict) -> bool:
    """NSFW-комментарий: Reddit помечает его `over_18` (флаг поста/сабреддита в листинге
    комментариев); `link_over_18` и `subreddit_over18` — запасные варианты полей."""
    for key in ("over_18", "link_over_18", "subreddit_over18"):
        v = comment_data.get(key)
        if v is True or (isinstance(v, str) and v.strip().lower() == "true"):
            return True
    return False
