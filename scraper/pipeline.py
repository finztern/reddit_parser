import asyncio
import html
import json
import time

import aiohttp

from .constants import SERVER_HARD_BATCH_LIMIT, log
from .utils import iso_utc


def is_nsfw(data: dict, blocked_subs: frozenset[str] = frozenset()) -> bool:
    """NSFW-признак из самих данных Reddit: over_18 (пост или родительский пост комментария),
    quarantine, либо саб из чёрного списка (имена в нижнем регистре, без "r/")."""
    if data.get("over_18") is True or data.get("quarantine") is True:
        return True
    return bool(blocked_subs) and (data.get("subreddit") or "").lower() in blocked_subs


def _clean(text) -> str:
    """Reddit в JSON-ответе экранирует & < > как &amp; &lt; &gt; — без обратной замены ссылки с query-строкой
    (…?a=1&b=2) портятся. Один html.unescape — точная обратная операция (буквальный «&amp;» от пользователя
    приходит как «&amp;amp;» и возвращается в «&amp;»)."""
    return html.unescape(text or "")


_IMG_EXT = {"jpeg": "jpg"}


def _media_url(media_id, meta) -> str:
    """Оригинал картинки/гифки из media_metadata -> https://i.redd.it/<id>.<ext> (стабильная ссылка, в отличие
    от preview.redd.it с подписью). Эмодзи/стикеры и невалидные записи пропускаются."""
    if not media_id or not isinstance(meta, dict) or meta.get("status") != "valid":
        return ""
    if meta.get("e") not in ("Image", "AnimatedImage"):
        return ""
    ext = str(meta.get("m") or "").partition("/")[2].lower()
    ext = _IMG_EXT.get(ext, ext)
    return f"https://i.redd.it/{media_id}.{ext}" if ext.isalnum() else ""


def _add_link(links: list[str], url) -> None:
    u = _clean(url).strip()
    if u.startswith(("http://", "https://")) and u not in links:
        links.append(u)


def comment_links(data: dict) -> list[str]:
    """Ссылки комментария, которых нет в тексте: картинки/гифки из media_metadata (в body они — ![img](id))."""
    links: list[str] = []
    for mid, meta in (data.get("media_metadata") or {}).items():
        _add_link(links, _media_url(mid, meta))
    return links


def post_links(data: dict) -> list[str]:
    """Все ссылки поста, не входящие в selftext: целевая ссылка/картинка/видео (у не-self поста), галерея,
    картинки внутри selftext, кроспост, превью Reddit. Требование заказчика: все ссылки обязаны попасть в content."""
    links: list[str] = []
    if not data.get("is_self"):
        _add_link(links, data.get("url_overridden_by_dest") or data.get("url"))
    meta = data.get("media_metadata") or {}
    gallery = (data.get("gallery_data") or {}).get("items") or []
    for it in gallery:
        _add_link(links, _media_url(it.get("media_id"), meta.get(it.get("media_id"))))
    for mid, m in meta.items():
        _add_link(links, _media_url(mid, m))
    for key in ("secure_media", "media"):
        _add_link(links, ((data.get(key) or {}).get("reddit_video") or {}).get("fallback_url"))
    for cp in data.get("crosspost_parent_list") or []:
        _add_link(links, cp.get("url_overridden_by_dest") or cp.get("url"))
    # превью Reddit (картинка внешней статьи/ссылки, превью видео и гифок): в тексте поста их нет, а заказчик
    # требует ВСЕ ссылки в content. Исходный размер + gif/mp4-варианты; уменьшенные resolutions не берём.
    for img in (data.get("preview") or {}).get("images") or []:
        _add_link(links, (img.get("source") or {}).get("url"))
        for var in (img.get("variants") or {}).values():
            _add_link(links, (var.get("source") or {}).get("url"))
    return links


def with_links(text: str, links: list[str]) -> str:
    """text + ссылки, которых в нём ещё нет (по одной на строку); сам текст не меняется."""
    missing = [u for u in links if u not in text]
    if not missing:
        return text
    tail = "\n".join(missing)
    return f"{text}\n\n{tail}" if text else tail


def profile_url(author: str) -> str:
    return f"https://www.reddit.com/user/{author}" if author and author not in ("[deleted]", "[removed]") else ""


def _summary(d: dict) -> str:
    """Компактная JSON-строка, как в образце схемы: {"score":"1","upvote_ratio":"",...} (без пробелов)."""
    return json.dumps(d, separators=(",", ":"))


def build_payload(comment_data: dict) -> dict | None:
    comment_id = comment_data.get("id")
    if not comment_id:
        return None

    fullname = comment_data.get("name") or f"t1_{comment_id}"
    created_utc = comment_data.get("created_utc")
    if created_utc is None:
        return None

    body = with_links(_clean(comment_data.get("body", "")), comment_links(comment_data))
    author = comment_data.get("author", "") or ""
    permalink = comment_data.get("permalink", "")
    url = f"https://www.reddit.com{permalink}" if permalink else ""

    # Exorde-правило для Reddit: external_parent_id указывает либо на
    # родительский пост (t3_...), либо на родительский комментарий
    # (t1_...) — раньше top-level комментарии (parent = пост, t3_)
    # получали пустую строку, что давало неполную схему и риск отсева/
    # понижения quality score. Теперь оба префикса сохраняются как есть;
    # всё остальное (пусто, мусор, неожиданный префикс) — по-прежнему "".
    # Ничего не выдумываем, если Reddit сам не отдал parent_id.
    parent_id = comment_data.get("parent_id", "") or ""
    external_parent_id = parent_id if parent_id.startswith(("t1_", "t3_")) else ""

    # communityName в формате "r/<sub>", как в примере с дашборда
    # (Exorde ожидает summary.communityName именно с префиксом "r/").
    subreddit = comment_data.get("subreddit") or ""
    community_name = f"r/{subreddit}" if subreddit else ""

    # summary — компактная JSON-строка с метаданными поста/комментария.
    # upvote_ratio и num_comments у Reddit есть только у постов (t3_),
    # не у комментариев (t1_) — для комментария оставляем их пустыми,
    # а не подставляем выдуманные значения.
    summary = _summary({
        "score": str(comment_data.get("score", "")),
        "upvote_ratio": "",
        "num_comments": "",
        "communityName": community_name,
        "data_type": "comment",
    })

    return {
        "content": body,
        "external_id": fullname,
        "created_at": iso_utc(created_utc),
        "domain": "reddit.com",
        "url": url,
        "title": "",
        "author": author,
        "username": author,
        "userprofile_url": profile_url(author),
        "external_parent_id": external_parent_id,
        "summary": summary,
        "_age_seconds": time.time() - created_utc,
        "_created_utc": float(created_utc),
    }


def build_post_payload(post_data: dict) -> dict | None:
    """Пост (t3_) в том же формате, что и комментарий; summary.data_type="post".

    content — текст поста (selftext); у ссылочных/картиночных постов он пуст, тогда берём заголовок,
    чтобы content не был пустым. К content ДОБАВЛЯЮТСЯ все ссылки поста (целевой URL, картинки, галерея, видео),
    которых нет в тексте. title — заголовок. external_parent_id у поста пуст."""
    post_id = post_data.get("id")
    if not post_id:
        return None
    created_utc = post_data.get("created_utc")
    if created_utc is None:
        return None

    fullname = post_data.get("name") or f"t3_{post_id}"
    title = _clean(post_data.get("title", ""))
    selftext = _clean(post_data.get("selftext", ""))
    if selftext.strip() in ("[removed]", "[deleted]"):
        selftext = ""
    author = post_data.get("author", "") or ""
    permalink = post_data.get("permalink", "")
    url = f"https://www.reddit.com{permalink}" if permalink else ""
    subreddit = post_data.get("subreddit") or ""
    community_name = f"r/{subreddit}" if subreddit else ""

    ratio = post_data.get("upvote_ratio")
    summary = _summary({
        "score": str(post_data.get("score", "")),
        "upvote_ratio": "" if ratio is None else str(ratio),
        "num_comments": str(post_data.get("num_comments", "")),
        "communityName": community_name,
        "data_type": "post",
    })

    return {
        "content": with_links(selftext or title, post_links(post_data)),
        "external_id": fullname,
        "created_at": iso_utc(created_utc),
        "domain": "reddit.com",
        "url": url,
        "title": title,
        "author": author,
        "username": author,
        "userprofile_url": profile_url(author),
        "external_parent_id": "",
        "summary": summary,
        "_age_seconds": time.time() - created_utc,
        "_created_utc": float(created_utc),
    }


def _item_ok(entry) -> bool:
    """Разбирает один элемент results[] из ответа /store_items.
    Формат отдельного результата коллектором явно не специфицирован,
    поэтому распознаём несколько разумных вариантов и по умолчанию
    считаем успехом, если явного признака ошибки нет."""
    if isinstance(entry, bool):
        return entry
    if isinstance(entry, dict):
        for key in ("ok", "success", "stored", "saved"):
            if key in entry:
                return bool(entry[key])
        if "error" in entry and entry["error"]:
            return False
        return True
    # неизвестный тип — не считаем это ошибкой, чтобы не заспамить логи
    return True


async def send_batch_to_store(
    session: aiohttp.ClientSession,
    endpoint: str,
    items: list[dict],
    account_name: str,
    batch_max_items: int,
) -> list[bool]:
    """Отправляет items одним (или несколькими, если батч больше лимита)
    POST-запросом(и) на /store_items. Возвращает список bool — по одному
    на исходный items[i], в том же порядке. False для элемента означает
    "не подтверждён как сохранённый" — вызывающий код обязан вызвать
    seen.release() для таких элементов, а не считать их отправленными."""
    if not items:
        return []

    chunk_size = max(1, min(batch_max_items, SERVER_HARD_BATCH_LIMIT))
    results: list[bool] = []

    for start in range(0, len(items), chunk_size):
        chunk = items[start:start + chunk_size]
        body = [{k: v for k, v in p.items() if not k.startswith("_")} for p in chunk]

        try:
            async with session.post(
                endpoint, json=body, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 413:
                    text = await resp.text()
                    log.error(
                        "[%s] store_items ответил 413 (батч %d элементов слишком большой) — "
                        "уменьши batch_max_items в config.yaml: %s",
                        account_name, len(chunk), text[:200],
                    )
                    results.extend([False] * len(chunk))
                    continue

                if resp.status >= 300:
                    text = await resp.text()
                    log.warning(
                        "[%s] store_items ответил %s для батча из %d: %s",
                        account_name, resp.status, len(chunk), text[:200],
                    )
                    results.extend([False] * len(chunk))
                    continue

                try:
                    data = await resp.json(content_type=None)
                except (json.JSONDecodeError, ValueError) as e:
                    # 2xx, но тело не распарсилось как JSON (например,
                    # прокси/балансировщик перед store_endpoint подменил
                    # тело ответа). Раз сервер не подтвердил сохранение
                    # поэлементно — считаем весь чанк неподтверждённым,
                    # а не роняем воркер необработанным исключением: он
                    # уйдёт по обычному пути seen.release() и повторной
                    # отправки в следующем цикле опроса.
                    log.warning(
                        "[%s] store_items вернул %s, но тело не распарсилось как JSON: %s",
                        account_name, resp.status, e,
                    )
                    results.extend([False] * len(chunk))
                    continue
                item_results = data.get("results") if isinstance(data, dict) else None

                if isinstance(item_results, list) and len(item_results) == len(chunk):
                    results.extend(_item_ok(r) for r in item_results)
                else:
                    # Сервер принял запрос, но не вернул results поэлементно —
                    # считаем весь чанк успешным по факту получения 2xx.
                    received = data.get("received") if isinstance(data, dict) else None
                    if received is not None and received != len(chunk):
                        log.warning(
                            "[%s] store_items: received=%s, но отправлено %d — часть могла не сохраниться",
                            account_name, received, len(chunk),
                        )
                    results.extend([True] * len(chunk))

        except aiohttp.ClientError as e:
            log.warning("[%s] Ошибка отправки батча в store_items (%s): %s", account_name, endpoint, e)
            results.extend([False] * len(chunk))
        except asyncio.TimeoutError:
            log.warning("[%s] Таймаут отправки батча в store_items (%s)", account_name, endpoint)
            results.extend([False] * len(chunk))

    return results
