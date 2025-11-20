# Extractor/modules/freecp.py
# Updated freecp module — robust Classplus URL extraction & fixes (2025-11-20)
# Drop this file into Extractor/modules/ and use as before.

import asyncio
import logging
import re
import os
from typing import List, Tuple, Dict, Any, Optional
import aiohttp
import time

# If your project exposes `app` or config, keep the import (used by your bot)
try:
    from Extractor import app
except Exception:
    app = None

# Optional config fallback for token/etc — keep safe defaults
FALLBACK_X_ACCESS_TOKEN = ""  # put a fallback token string if you need one (not mandatory)

# Configure logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("freecp")

# --- Helper functions -------------------------------------------------------

def _is_playable_url(url: str) -> bool:
    """Return True if URL already looks like a playable media manifest/file."""
    if not url:
        return False
    play_ext = (".m3u8", ".mpd", ".mp4", ".ism/manifest", ".mkv")
    return any(url.lower().endswith(ext) for ext in play_ext) or "playlist.m3u8" in url

def _safe_join(*parts: str) -> str:
    """Join parts ignoring empty ones."""
    return "".join(p for p in parts if p)

# Patterns & conversion logic derived from Classplus CDN behaviour
def transform_thumbnail_to_manifest(url: str) -> str:
    """
    Try to convert thumbnail/png/jpg URLs to a likely master/playlist manifest.
    This covers common patterns observed on Classplus / tb-video / testbook.
    """
    if not url:
        return url

    # Quick return if already playable
    if _is_playable_url(url):
        return url

    try:
        # tencdn / media-cdn Tencent style:
        # https://media-cdn.classplusapp.com/tencent/{id}/thumbnail.png  -> .../{id}/master.m3u8
        if "media-cdn.classplusapp.com/tencent/" in url or "tencdn.classplusapp.com" in url:
            base = url.rsplit('/', 1)[0]
            return f"{base}/master.m3u8"

        # tb-video (older pattern)
        # https://tb-video.classplusapp.com/{videoid}.jpg  -> .../{videoid}/master.m3u8
        m = re.search(r"(tb-video\.classplusapp\.com/)([a-f0-9\-]+)\.jpg", url)
        if m:
            vid = m.group(2)
            return f"https://tb-video.classplusapp.com/{vid}/master.m3u8"

        # Standard classplus thumbnail that includes cc/ identifier
        # e.g. https://media-cdn.classplusapp.com/1005566/cc/<id>-xx/thumbnail.png
        # Convert thumbnail.png -> master.m3u8
        if url.endswith("thumbnail.png") or url.endswith("thumbnail.jpg"):
            return url.rsplit('/', 1)[0] + "/master.m3u8"

        # Some urls embed id as a filename with hash; try to detect 24/32 char ids
        m2 = re.search(r"/([a-f0-9]{20,64})\.(?:jpeg|jpg|png)$", url)
        if m2:
            identifier = m2.group(1)
            # generic fallback path used earlier in repo
            return f"https://media-cdn.classplusapp.com/alisg-cdn-a.classplusapp.com/{identifier}/master.m3u8"

        # drm folder thumbnails -> drm playlist
        if "/drm/" in url and url.endswith(".png"):
            parts = url.split('/')
            # pick last non-empty third-from-last as id if present
            if len(parts) >= 4:
                vid = parts[-3]
                return f"https://media-cdn.classplusapp.com/drm/{vid}/playlist.m3u8"

    except Exception:
        # return original on any unexpected failure
        pass

    # If no transform matched, return original (caller will decide)
    return url

# --- Signed URL fetching with retries --------------------------------------

async def fetch_signed_url(session: aiohttp.ClientSession, url_val: str, headers: Dict[str, str], max_retries: int = 4, timeout: int = 25) -> Optional[str]:
    """
    Call the Classplus jw-signed-url endpoint to resolve thumbnails -> playable manifests
    Returns resolved URL (str) or None on failure.
    """
    api = "https://api.classplusapp.com/cams/uploader/video/jw-signed-url"
    params = {"url": url_val}

    # If headers don't contain an 'x-access-token' supply fallback if available
    headers = headers.copy() if headers else {}
    if "x-access-token" not in {k.lower(): v for k, v in headers.items()} and FALLBACK_X_ACCESS_TOKEN:
        headers.setdefault("x-access-token", FALLBACK_X_ACCESS_TOKEN)

    backoff = 1
    for attempt in range(1, max_retries + 1):
        try:
            async with session.get(api, params=params, headers=headers, timeout=timeout) as resp:
                # 429 or 5xx -> retry with backoff
                if resp.status in (429, 500, 502, 503, 504):
                    logger.warning(f"Signed-url rate/server ({resp.status}) for {url_val} attempt {attempt}/{max_retries}")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 20)
                    continue

                # If 200, try parse JSON, otherwise assume not resolvable
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except Exception:
                        text = await resp.text()
                        logger.debug(f"Signed-url non-json response: {text[:200]}")
                        return None

                    # Common shapes: {"url": "..."} or {"drmUrls": {"manifestUrl": "..."}}
                    signed = data.get("url") or (data.get("drmUrls") and data.get("drmUrls").get("manifestUrl"))
                    if signed:
                        return signed
                    # Some responses may embed other keys
                    for candidate in ("url", "manifestUrl", "signedUrl"):
                        v = data.get(candidate)
                        if v:
                            return v
                    # nothing resolvable
                    logger.debug(f"Signed-url returned no usable field for {url_val}: {data}")
                    return None
                else:
                    # non-200: read body for debug, then decide to retry or not
                    body = await resp.text()
                    logger.debug(f"Signed-url status {resp.status} body: {body[:200]}")
                    # if client error like 400/403/404 -> not retry
                    if 400 <= resp.status < 500:
                        return None
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 20)
        except asyncio.TimeoutError:
            logger.warning(f"Signed-url timeout for {url_val} attempt {attempt}/{max_retries}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 20)
        except Exception as e:
            logger.exception(f"Unexpected error fetching signed-url for {url_val}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 20)

    logger.error(f"Failed to fetch signed-url for {url_val} after {max_retries} attempts")
    return None

# --- Processing single content item ---------------------------------------

async def process_single_content_url(name: str, raw_url: str, session: aiohttp.ClientSession, headers: Dict[str, str]) -> Optional[str]:
    """
    Given a content name & raw URL (thumbnail or url field), return a string "Name:ResolvedURL\n"
    or None if nothing useful found.
    """
    if not raw_url:
        return None

    raw_url = raw_url.strip()

    # If looks playable already -> return as-is
    if _is_playable_url(raw_url):
        return f"{name}:{raw_url}\n"

    # Try to transform thumbnail to manifest
    candidate = transform_thumbnail_to_manifest(raw_url)

    # If transformed to a playable extension, return
    if candidate and _is_playable_url(candidate):
        return f"{name}:{candidate}\n"

    # As a fallback, attempt to hit signed-url endpoint (only if it looks like a classplus media-cdn path)
    # Avoid hitting signed-url for every arbitrary image URL; restrict to known host substrings.
    if any(host in raw_url for host in ("media-cdn.classplusapp.com", "tb-video.classplusapp.com", "cpvideocdn.testbook.com", "tencdn.classplusapp.com", "classplusapp.com/drm")):
        try:
            signed = await fetch_signed_url(session, raw_url, headers=headers)
            if signed:
                return f"{name}:{signed}\n"
        except Exception:
            logger.exception("Error while fetching signed url")

    # Could not resolve to playable manifest — still return original (some downstream flows expect it)
    return f"{name}:{raw_url}\n"

# --- Core listing function (public) ----------------------------------------

async def get_cpwp_course_content(session: aiohttp.ClientSession, headers: Dict[str, str], Batch_Token: str,
                                  folder_id: int = 0, limit: int = 9999999999, retry_count: int = 0
                                  ) -> Tuple[List[str], int, int, int]:
    """
    Recursively fetch course preview content for a Classplus Batch_Token.
    Returns (results_list, video_count, pdf_count, image_count)
    Each results_list item is "Name:URL\n" (URL ideally playable manifest if resolvable).
    """
    MAX_RETRIES = 4
    TIMEOUT = 30

    results: List[str] = []
    video_count = 0
    pdf_count = 0
    image_count = 0
    fetched_urls = set()

    content_api = f"https://api.classplusapp.com/v2/course/preview/content/list/{Batch_Token}"
    params = {"folderId": folder_id, "limit": limit}

    try:
        async with session.get(content_api, params=params, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status == 429:
                # Encourage caller to retry; here we do a limited retry
                if retry_count < MAX_RETRIES:
                    await asyncio.sleep(min(2 ** retry_count, 20))
                    return await get_cpwp_course_content(session, headers, Batch_Token, folder_id, limit, retry_count + 1)
                else:
                    logger.error("Rate-limited on content list and max retries exhausted")
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

    # We'll process items in controlled concurrent batches to avoid overloading signed-url endpoint.
    tasks: List[asyncio.Task] = []
    folder_tasks: List[Tuple[int, asyncio.Task]] = []

    # local session for signed url calls uses same session provided
    for content in contents:
        ctype = content.get("contentType")
        cid = content.get("id")
        name = content.get("name", "Untitled")
        # prefer 'url' first, then thumbnailUrl then thumbnail
        raw_url = content.get("url") or content.get("thumbnailUrl") or content.get("thumbnail") or ""
        # If the content is a folder, schedule recursive processing
        if ctype == 1:
            folder_tasks.append((cid, asyncio.create_task(get_cpwp_course_content(session, headers, Batch_Token, folder_id=cid, limit=limit))))
            continue

        # Some content returns image URL even for video thumbnails; transform patterns into manifests
        transformed = transform_thumbnail_to_manifest(raw_url)

        # avoid duplicates
        key = transformed or raw_url
        if not key:
            # nothing to process for this content
            continue
        if key in fetched_urls:
            continue
        fetched_urls.add(key)

        # Create a task to process and resolve the URL (makes signed-url calls only when necessary)
        tasks.append(asyncio.create_task(process_single_content_url(name, raw_url, session, headers)))

    # Execute content tasks in controlled chunks (max concurrency)
    CHUNK = 12
    for i in range(0, len(tasks), CHUNK):
        chunk = tasks[i:i + CHUNK]
        try:
            done = await asyncio.gather(*chunk, return_exceptions=True)
            for item in done:
                if isinstance(item, Exception):
                    logger.error(f"Content task error: {item}")
                elif item:
                    results.append(item)
                    if _is_playable_url(item.split(":", 1)[1].strip()):
                        video_count += 1
                    elif item.lower().endswith(".pdf\n") or ".pdf" in item.lower():
                        pdf_count += 1
                    else:
                        image_count += 1
        except Exception as e:
            logger.exception(f"Error processing content chunk: {e}")
        await asyncio.sleep(0.2)

    # Process folder recursion results (gather results from nested calls)
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

# --- Minimal handler usable by your bot ------------------------------------

async def process_cpwp(bot, m, user_id: int):
    """
    A minimal wrapper that prompts for org_code (if you call it directly from bot),
    fetches batches, and returns extracted content.
    This keeps behavior similar to your previous freecp module.
    """
    # This function is intentionally lightweight and expects your bot's existing flow
    # to call get_cpwp_course_content after deriving Batch_Token from the org/course.
    await m.reply_text("Use your existing CP flow — this module focuses on URL extraction (get_cpwp_course_content).")

# If run directly for quick test (not required for bot), allow a simple command-line test:
if __name__ == "__main__":
    import argparse, asyncio

    async def _quick_test(url: str):
        async with aiohttp.ClientSession() as session:
            h = {'user-agent': 'Mobile-Android', 'api-version': '35'}
            # try a transform + signed url resolution
            transformed = transform_thumbnail_to_manifest(url)
            print("transformed ->", transformed)
            signed = await fetch_signed_url(session, url, h)
            print("signed ->", signed)

    parser = argparse.ArgumentParser()
    parser.add_argument("--test-url", help="Test thumbnail or media url", required=False)
    args = parser.parse_args()
    if args.test_url:
        asyncio.run(_quick_test(args.test_url))
    else:
        print("freecp.py module loaded — import get_cpwp_course_content(session, headers, Batch_Token) in your bot code.")
