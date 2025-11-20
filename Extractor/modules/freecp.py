# Extractor/modules/freecp.py
# Patched freecp module — robust Classplus URL extraction & signed-url fallback (2025-11-20)
# Replace existing Extractor/modules/freecp.py with this file.

import asyncio
import logging
import re
import os
import time
from typing import List, Tuple, Dict, Any, Optional
import aiohttp

# If your project exposes `app` or config, keep the import (used by your bot)
try:
    from Extractor import app
except Exception:
    app = None

# Optional fallback token (leave empty or set in config if you want)
FALLBACK_X_ACCESS_TOKEN = ""

# logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("freecp")

# -------------------- helpers --------------------

def _is_playable_url(url: str) -> bool:
    """Return True if URL already looks like a playable media manifest/file."""
    if not url:
        return False
    u = url.lower()
    play_ext = (".m3u8", ".mpd", ".mp4", ".ism/manifest", ".mkv", "playlist.m3u8", "manifest.mpd")
    return any(u.endswith(ext) for ext in play_ext) or "playlist.m3u8" in u or "manifest" in u and ("m3u8" in u or "mpd" in u)

def _safe_strip(u: Optional[str]) -> str:
    return (u or "").strip()

# Convert known thumbnail/image URL patterns to likely master/playlist manifests
def transform_thumbnail_to_manifest(url: str) -> str:
    """Try to convert thumbnail/png/jpg URLs to likely master/playlist manifest URLs.

    This function encodes the common transformation rules observed in Classplus / tb-video / testbook CDNs.
    If no transformation matched, returns original url (caller decides).
    """
    if not url:
        return url
    url = url.strip()

    # If already playable, return as-is
    if _is_playable_url(url):
        return url

    try:
        # Tencent style / media-cdn tencent subpath:
        # https://media-cdn.classplusapp.com/tencent/{id}/thumbnail.png  -> .../{id}/master.m3u8
        if "media-cdn.classplusapp.com/tencent/" in url or "tencdn.classplusapp.com" in url:
            base = url.rsplit('/', 1)[0]
            return base + "/master.m3u8"

        # tb-video older pattern:
        # https://tb-video.classplusapp.com/{videoid}.jpg  -> https://tb-video.classplusapp.com/{videoid}/master.m3u8
        m = re.search(r"(tb-video\.classplusapp\.com)/([a-f0-9\-]+)\.(?:jpg|jpeg|png)$", url, flags=re.I)
        if m:
            vid = m.group(2)
            return f"https://{m.group(1)}/{vid}/master.m3u8"

        # thumbnail.png / thumbnail.jpg -> replace with master.m3u8
        if url.endswith("thumbnail.png") or url.endswith("thumbnail.jpg") or url.endswith("thumbnail.jpeg"):
            return url.rsplit('/', 1)[0] + "/master.m3u8"

        # drm folder thumbnails -> drm playlist
        # e.g. https://media-cdn.classplusapp.com/drm/<id>/thumbnail.png  -> https://media-cdn.classplusapp.com/drm/<id>/playlist.m3u8
        if "/drm/" in url and (url.endswith(".png") or url.endswith(".jpg") or url.endswith(".jpeg")):
            parts = [p for p in url.split("/") if p]
            # pick id as third-from-last if structure matches
            if len(parts) >= 3:
                # find index of 'drm' then id next to it
                try:
                    idx = parts.index("drm")
                    if idx + 1 < len(parts):
                        vid = parts[idx + 1]
                        return f"https://media-cdn.classplusapp.com/drm/{vid}/playlist.m3u8"
                except ValueError:
                    pass

        # Some urls embed long hex/hash as filename; fallback path used earlier in repo
        m2 = re.search(r"/([a-f0-9]{20,64})\.(?:jpeg|jpg|png)$", url, flags=re.I)
        if m2:
            identifier = m2.group(1)
            # generic fallback path used in many repos
            return f"https://media-cdn.classplusapp.com/alisg-cdn-a.classplusapp.com/{identifier}/master.m3u8"

        # testbook style: cpvideocdn.testbook.com/.../thumbnail.png -> cpvod.testbook.com/{id}/playlist.m3u8
        if "cpvideocdn.testbook.com" in url and (url.endswith(".png") or url.endswith(".jpg")):
            match = re.search(r"/streams/([a-f0-9]{20,32})/", url, flags=re.I)
            if match:
                vid = match.group(1)
                return f"https://cpvod.testbook.com/{vid}/playlist.m3u8"
            # fallback: try second last path segment as id
            parts = url.split("/")
            if len(parts) >= 2:
                vid = parts[-2] or parts[-3] if len(parts) >= 3 else None
                if vid:
                    return f"https://cpvod.testbook.com/{vid}/playlist.m3u8"

    except Exception as e:
        logger.exception(f"transform_thumbnail_to_manifest failed for {url}: {e}")

    # no transform matched, return original
    return url

# -------------------- signed url resolver --------------------

async def fetch_signed_url(session: aiohttp.ClientSession, url_val: str, headers: Dict[str, str], max_retries: int = 4, timeout: int = 20) -> Optional[str]:
    """
    Call the Classplus jw-signed-url endpoint to resolve thumbnails -> playable manifests.
    Returns resolved URL (str) or None on failure.
    """
    api = "https://api.classplusapp.com/cams/uploader/video/jw-signed-url"
    params = {"url": url_val}

    # copy headers; ensure token present if fallback available
    hdrs = {k: v for k, v in (headers or {}).items()}
    # case-insensitive check
    if not any(k.lower() == "x-access-token" for k in hdrs.keys()) and FALLBACK_X_ACCESS_TOKEN:
        hdrs["x-access-token"] = FALLBACK_X_ACCESS_TOKEN

    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            async with session.get(api, params=params, headers=hdrs, timeout=timeout) as resp:
                # rate limit / server error => retry
                if resp.status in (429, 500, 502, 503, 504):
                    logger.warning(f"signed-url status {resp.status} for {url_val}; attempt {attempt}/{max_retries}")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 20)
                    continue

                # success -> parse
                if resp.status == 200:
                    try:
                        body = await resp.json()
                    except Exception:
                        txt = await resp.text()
                        logger.debug(f"signed-url nonjson body: {txt[:300]}")
                        return None

                    # common shapes for manifests
                    signed = body.get("url") or (body.get("drmUrls") and body.get("drmUrls").get("manifestUrl"))
                    if signed:
                        return signed

                    # fallback lookups
                    for key in ("manifestUrl", "signedUrl", "resolvedUrl", "url"):
                        v = body.get(key)
                        if v:
                            return v

                    # nothing useful found
                    logger.debug(f"signed-url returned no usable field for {url_val}: {body}")
                    return None
                else:
                    text = await resp.text()
                    logger.debug(f"signed-url {resp.status} for {url_val} body: {text[:300]}")
                    # client errors (400-499) likely mean no resolution possible; don't aggressively retry
                    if 400 <= resp.status < 500:
                        return None
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 20)
        except asyncio.TimeoutError:
            logger.warning(f"signed-url timeout for {url_val} attempt {attempt}/{max_retries}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 20)
        except Exception as e:
            logger.exception(f"Unexpected error resolving signed-url for {url_val}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 20)

    logger.error(f"Failed to fetch signed-url for {url_val} after {max_retries} attempts")
    return None

# -------------------- single content processing --------------------

async def process_single_content_url(name: str, raw_url: str, session: aiohttp.ClientSession, headers: Dict[str, str]) -> Optional[str]:
    """
    Given content name & raw_url (thumbnail or url field), return "Name:ResolvedURL\n".
    Prefer pattern transforms first (fast), if not playable then try signed-url fallback for known hosts.
    """
    if not raw_url:
        return None
    raw_url = raw_url.strip()

    # If already playable manifest -> return
    if _is_playable_url(raw_url):
        return f"{name}:{raw_url}\n"

    # Try pattern transform
    candidate = transform_thumbnail_to_manifest(raw_url)
    if candidate and _is_playable_url(candidate):
        return f"{name}:{candidate}\n"

    # Restrict signed-url calls to known hosts (avoid calling for arbitrary images)
    known_hosts = ("media-cdn.classplusapp.com", "tb-video.classplusapp.com", "cpvideocdn.testbook.com", "tencdn.classplusapp.com", "/drm/")
    if any(h in raw_url for h in known_hosts):
        try:
            signed = await fetch_signed_url(session, raw_url, headers=headers)
            if signed:
                return f"{name}:{signed}\n"
        except Exception:
            logger.exception("signed-url fallback failed")

    # if nothing resolved, still return original (some downstream flows expect original)
    return f"{name}:{raw_url}\n"

# -------------------- public recursive listing --------------------

async def get_cpwp_course_content(session: aiohttp.ClientSession, headers: Dict[str, str], Batch_Token: str,
                                  folder_id: int = 0, limit: int = 9999999999, retry_count: int = 0
                                  ) -> Tuple[List[str], int, int, int]:
    """
    Recursively fetch course preview content for a Classplus Batch_Token.
    Returns (results_list, video_count, pdf_count, image_count)
    Each results_list item is "Name:URL\n" (URL ideally playable manifest if resolvable).
    """
    MAX_RETRIES = 4
    TIMEOUT = 25

    results: List[str] = []
    video_count = 0
    pdf_count = 0
    image_count = 0
    fetched_keys = set()

    content_api = f"https://api.classplusapp.com/v2/course/preview/content/list/{Batch_Token}"
    params = {"folderId": folder_id, "limit": limit}

    try:
        async with session.get(content_api, params=params, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status == 429:
                if retry_count < MAX_RETRIES:
                    await asyncio.sleep(min(2 ** retry_count, 20))
                    return await get_cpwp_course_content(session, headers, Batch_Token, folder_id, limit, retry_count + 1)
                logger.error("Rate-limited and retries exhausted when fetching content list")
                return [], 0, 0, 0

            resp.raise_for_status()
            body = await resp.json()
            contents = body.get("data", []) or []
    except asyncio.TimeoutError:
        if retry_count < MAX_RETRIES:
            await asyncio.sleep(min(2 ** retry_count, 20))
            return await get_cpwp_course_content(session, headers, Batch_Token, folder_id, limit, retry_count + 1)
        logger.exception("Timeout while fetching content list")
        return [], 0, 0, 0
    except Exception as e:
        logger.exception(f"Error retrieving content list: {e}")
        return [], 0, 0, 0

    # Prepare tasks
    content_tasks: List[asyncio.Task] = []
    folder_tasks: List[Tuple[int, asyncio.Task]] = []

    for content in contents:
        ctype = content.get("contentType")
        cid = content.get("id")
        name = content.get("name", "Untitled")
        raw_url = content.get("url") or content.get("thumbnailUrl") or content.get("thumbnail") or ""

        # If folder -> recursive
        if ctype == 1:
            folder_tasks.append((cid, asyncio.create_task(get_cpwp_course_content(session, headers, Batch_Token, folder_id=cid, limit=limit))))
            continue

        # Avoid reprocessing same raw string transformed result
        key = transform_thumbnail_to_manifest(raw_url) or raw_url
        if not key:
            continue
        if key in fetched_keys:
            continue
        fetched_keys.add(key)

        # Create task for resolving single item
        content_tasks.append(asyncio.create_task(process_single_content_url(name, raw_url, session, headers)))

    # Run tasks in controlled concurrency chunks to avoid hitting signed-url rate limits
    CHUNK = 12
    for i in range(0, len(content_tasks), CHUNK):
        chunk = content_tasks[i:i + CHUNK]
        try:
            done = await asyncio.gather(*chunk, return_exceptions=True)
            for item in done:
                if isinstance(item, Exception):
                    logger.error(f"Content task error: {item}")
                    continue
                if not item:
                    continue
                results.append(item)
                # quick type detection
                _, url_part = item.split(":", 1)
                url_stripped = url_part.strip().lower()
                if _is_playable_url(url_stripped):
                    video_count += 1
                elif ".pdf" in url_stripped:
                    pdf_count += 1
                else:
                    image_count += 1
        except Exception as e:
            logger.exception(f"Error processing chunk: {e}")
        await asyncio.sleep(0.2)

    # gather folder results
    for fid, ftask in folder_tasks:
        try:
            sub_results, sub_v, sub_p, sub_i = await ftask
            if sub_results:
                results.extend(sub_results)
            video_count += sub_v
            pdf_count += sub_p
            image_count += sub_i
        except Exception as e:
            logger.exception(f"Folder task {fid} failed: {e}")

    return results, video_count, pdf_count, image_count

# -------------------- minimal wrapper used by your bot --------------------

async def process_cpwp(bot, m, user_id: int):
    """
    Minimal wrapper kept for compatibility with your bot.
    This module's main focus is get_cpwp_course_content(session, headers, Batch_Token)
    which returns fully-transformed URLs when possible.
    """
    await m.reply_text("freecp module active — call existing flow which derives Batch_Token and calls get_cpwp_course_content().")

# -------------------- quick CLI test --------------------
if __name__ == "__main__":
    import argparse, asyncio

    async def _quick_test(url: str):
        async with aiohttp.ClientSession() as session:
            h = {'user-agent': 'Mobile-Android', 'api-version': '35'}
            print("orig  ->", url)
            print("trans ->", transform_thumbnail_to_manifest(url))
            signed = await fetch_signed_url(session, url, h)
            print("signed ->", signed)

    p = argparse.ArgumentParser()
    p.add_argument("--test-url", help="thumbnail or media url", required=False)
    args = p.parse_args()
    if args.test_url:
        asyncio.run(_quick_test(args.test_url))
    else:
        print("freecp module ready. import get_cpwp_course_content(session, headers, Batch_Token) in your bot code.")
