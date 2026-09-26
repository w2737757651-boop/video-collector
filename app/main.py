import asyncio
import html
import json
import re
import socket
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from yt_dlp import YoutubeDL

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Video Collector", version="2.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)

ALLOWED_SOURCE_HOSTS = (
    "douyin.com",
    "iesdouyin.com",
    "xiaohongshu.com",
    "xhslink.com",
    "xhslink.cn",
    "kuaishou.com",
    "bilibili.com",
    "b23.tv",
    "weixin.qq.com",
)

UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Mobile Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PRIVATE_NETS = (
    "127.", "10.", "192.168.", "169.254.",
    "0.", "224.", "240.",
)


class ParseBody(BaseModel):
    text: str


def extract_url(text: str) -> str:
    m = URL_RE.search((text or "").strip())
    if not m:
        raise ValueError("没有识别到有效链接")
    return m.group(0).rstrip("，。,.；;）)")


def source_host_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in ALLOWED_SOURCE_HOSTS)


def validate_source_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("只支持 http/https 链接")
    if not source_host_allowed(url):
        raise ValueError("当前版本只支持抖音、小红书、快手、B站和微信相关分享链接")


def normalize_url(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = html.unescape(s).replace("\\u002F", "/").replace("\\/", "/")
    if s.startswith("//"):
        s = "https:" + s
    return s


def is_public_remote_url(url: str) -> bool:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        host = p.hostname.lower()
        if host == "localhost" or any(host.startswith(x) for x in PRIVATE_NETS):
            return False
        # Block obvious literal private IPv6/IPv4 targets.
        try:
            ip = socket.gethostbyname(host)
            if (
                ip.startswith("127.")
                or ip.startswith("10.")
                or ip.startswith("192.168.")
                or ip.startswith("169.254.")
                or ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31
            ):
                return False
        except Exception:
            pass
        return True
    except Exception:
        return False


async def fetch_html(url: str, referer: str | None = None) -> tuple[str, str]:
    headers = dict(BASE_HEADERS)
    if referer:
        headers["Referer"] = referer
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=20,
        headers=headers,
        http2=True,
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        return str(r.url), r.text


def extract_balanced_json(text: str, marker: str):
    pos = text.find(marker)
    if pos < 0:
        return None

    # Find the first JSON object/array after the marker.
    start_obj = text.find("{", pos + len(marker))
    start_arr = text.find("[", pos + len(marker))
    candidates = [x for x in (start_obj, start_arr) if x >= 0]
    if not candidates:
        return None
    start = min(candidates)

    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                raw = text[start:i + 1]
                raw = re.sub(r"\bundefined\b", "null", raw)
                try:
                    return json.loads(raw)
                except Exception:
                    return None
    return None


def iter_nodes(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_nodes(v)


def first_string_for_keys(obj, keys):
    for node in iter_nodes(obj):
        for k in keys:
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def first_number_for_keys(obj, keys):
    for node in iter_nodes(obj):
        for k in keys:
            v = node.get(k)
            if isinstance(v, (int, float)):
                return v
    return None


def collect_media_urls(obj, platform: str):
    found = []
    seen = set()

    def add(url, width=None, height=None, quality=None):
        url = normalize_url(url)
        if not url or not url.startswith(("http://", "https://")):
            return
        low = url.lower()

        if platform == "xiaohongshu":
            likely = (
                "xhscdn.com" in low
                or "sns-video" in low
                or ".mp4" in low
                or "video" in low and "xhs" in low
            )
        elif platform == "douyin":
            likely = (
                ".mp4" in low
                or "douyinvod" in low
                or "bytev" in low
                or "video" in low and ("douyin" in low or "snssdk" in low)
            )
        else:
            likely = ".mp4" in low or ".m3u8" in low

        if not likely or url in seen:
            return
        seen.add(url)
        if not is_public_remote_url(url):
            return

        if quality is None and height:
            quality = f"{height}p"

        found.append({
            "quality": quality or "视频源",
            "url": url,
            "ext": "m3u8" if ".m3u8" in low else "mp4",
            "width": width,
            "height": height,
        })

    for node in iter_nodes(obj):
        width = node.get("width") if isinstance(node.get("width"), int) else None
        height = node.get("height") if isinstance(node.get("height"), int) else None

        for key, value in node.items():
            kl = str(key).lower()

            if isinstance(value, str):
                if any(x in kl for x in ("url", "src", "play", "video", "stream", "master")):
                    add(value, width, height)

            elif isinstance(value, list) and any(
                x in kl for x in ("url", "play", "video", "stream", "master")
            ):
                for item in value:
                    if isinstance(item, str):
                        add(item, width, height)

    # Prefer larger known resolutions and then keep stable insertion order.
    found.sort(key=lambda x: ((x.get("height") or 0), (x.get("width") or 0)), reverse=True)
    return found


def platform_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "douyin" in host or "iesdouyin" in host:
        return "douyin"
    if "xiaohongshu" in host or "xhslink" in host:
        return "xiaohongshu"
    if "bilibili" in host or "b23.tv" in host:
        return "bilibili"
    if "kuaishou" in host:
        return "kuaishou"
    if "weixin.qq.com" in host:
        return "wechat"
    return "generic"


async def resolve_public_share(url: str) -> str:
    """
    Resolve a public share link. If Xiaohongshu redirects to a login page that
    includes the original public note in redirectPath, recover that target URL.
    This does not bypass the login page or use account credentials.
    """
    headers = dict(BASE_HEADERS)
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers=headers) as client:
        r = await client.get(url)
        final_url = str(r.url)

    p = urlparse(final_url)
    if "xiaohongshu.com" in (p.hostname or "") and p.path.startswith("/login"):
        qs = parse_qs(p.query)
        redirect_path = qs.get("redirectPath", [None])[0]
        if redirect_path:
            candidate = unquote(redirect_path)
            if candidate.startswith("http") and source_host_allowed(candidate):
                return candidate

    return final_url


async def parse_xiaohongshu(source_url: str):
    direct = await resolve_public_share(source_url)
    final_url, page = await fetch_html(direct)

    if "/login" in final_url or "captcha" in final_url.lower():
        raise ValueError("小红书公开页面当前要求登录或验证码，未尝试绕过访问限制。")

    state = (
        extract_balanced_json(page, "window.__INITIAL_STATE__")
        or extract_balanced_json(page, "__INITIAL_STATE__")
    )
    if not state:
        raise ValueError("小红书页面已打开，但没有找到公开的 __INITIAL_STATE__ 数据。")

    videos = collect_media_urls(state, "xiaohongshu")
    if not videos:
        raise ValueError("已读取小红书公开页面，但没有找到可直接访问的视频资源。")

    title = first_string_for_keys(state, ("title", "displayTitle", "desc", "description"))
    author = first_string_for_keys(state, ("nickname", "nickName", "name"))
    cover = first_string_for_keys(state, ("cover", "coverUrl", "image", "imageUrl"))
    duration = first_number_for_keys(state, ("duration", "videoDuration"))

    return {
        "platform": "XiaoHongShu",
        "id": first_string_for_keys(state, ("noteId", "id")),
        "title": title or "小红书视频",
        "author": author,
        "cover": normalize_url(cover or "") or None,
        "duration": duration,
        "source_url": source_url,
        "resolved_url": final_url,
        "referer": final_url,
        "videos": videos[:12],
    }


async def parse_douyin(source_url: str):
    direct = await resolve_public_share(source_url)
    final_url, page = await fetch_html(direct)

    if "/login" in final_url or "captcha" in final_url.lower():
        raise ValueError("抖音公开页面当前要求登录或验证码，未尝试绕过访问限制。")

    state = (
        extract_balanced_json(page, "window._ROUTER_DATA")
        or extract_balanced_json(page, "_ROUTER_DATA")
        or extract_balanced_json(page, "__INITIAL_STATE__")
        or extract_balanced_json(page, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
    )

    # Older Douyin pages may embed percent-encoded RENDER_DATA.
    if not state:
        m = re.search(r'id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>', page, re.S | re.I)
        if m:
            try:
                raw = unquote(html.unescape(m.group(1)))
                state = json.loads(raw)
            except Exception:
                state = None

    if not state:
        raise ValueError(
            "抖音页面已打开，但没有找到公开的页面状态数据。"
            "当前抖音可能已对该链接启用额外验证。"
        )

    videos = collect_media_urls(state, "douyin")
    if not videos:
        raise ValueError("已读取抖音公开页面，但没有找到可直接访问的视频资源。")

    title = first_string_for_keys(state, ("desc", "title", "description"))
    author = first_string_for_keys(state, ("nickname", "uniqueId", "unique_id", "name"))
    cover = first_string_for_keys(state, ("cover", "coverUrl", "originCover", "dynamicCover"))
    duration = first_number_for_keys(state, ("duration",))

    # Douyin durations are sometimes milliseconds.
    if isinstance(duration, (int, float)) and duration > 10000:
        duration = round(duration / 1000, 3)

    return {
        "platform": "Douyin",
        "id": first_string_for_keys(state, ("awemeId", "aweme_id", "id")),
        "title": title or "抖音视频",
        "author": author,
        "cover": normalize_url(cover or "") or None,
        "duration": duration,
        "source_url": source_url,
        "resolved_url": final_url,
        "referer": final_url,
        "videos": videos[:12],
    }


def ytdlp_extract_sync(url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": 20,
        "retries": 1,
        "fragment_retries": 1,
    }
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


async def parse_with_ytdlp(url: str):
    info = await asyncio.wait_for(
        asyncio.to_thread(ytdlp_extract_sync, url),
        timeout=45,
    )

    if not info:
        raise ValueError("解析器没有返回视频数据")

    formats = []
    for f in info.get("formats") or []:
        media_url = f.get("url")
        if not media_url or f.get("vcodec") == "none":
            continue
        if not is_public_remote_url(media_url):
            continue
        formats.append({
            "quality": (
                f"{f.get('height')}p"
                if f.get("height")
                else f.get("format_note") or f.get("format_id")
            ),
            "url": media_url,
            "ext": f.get("ext"),
            "width": f.get("width"),
            "height": f.get("height"),
        })

    formats.sort(
        key=lambda x: ((x.get("height") or 0), (x.get("width") or 0)),
        reverse=True,
    )

    return {
        "platform": info.get("extractor_key") or info.get("extractor"),
        "id": info.get("id"),
        "title": info.get("title"),
        "author": info.get("uploader") or info.get("channel") or info.get("creator"),
        "cover": info.get("thumbnail"),
        "duration": info.get("duration"),
        "source_url": url,
        "resolved_url": url,
        "referer": url,
        "videos": formats[:12],
    }


async def parse_any(source_url: str):
    validate_source_url(source_url)
    platform = platform_from_url(source_url)

    # Platform adapters first. yt-dlp is only the fallback.
    if platform == "douyin":
        try:
            return await parse_douyin(source_url)
        except Exception as adapter_error:
            try:
                return await parse_with_ytdlp(source_url)
            except Exception:
                raise ValueError(str(adapter_error))

    if platform == "xiaohongshu":
        try:
            return await parse_xiaohongshu(source_url)
        except Exception as adapter_error:
            try:
                return await parse_with_ytdlp(source_url)
            except Exception:
                raise ValueError(str(adapter_error))

    return await parse_with_ytdlp(source_url)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"ok": True, "version": "2.0"}


@app.post("/api/parse")
async def parse_video(body: ParseBody):
    try:
        source_url = extract_url(body.text)
        data = await parse_any(source_url)

        # Use a server-side download route. The source URL is reparsed by the
        # server, so the client cannot choose an arbitrary internal URL.
        for i, item in enumerate(data.get("videos") or []):
            item["download_url"] = (
                "/api/download?source="
                + quote(source_url, safe="")
                + "&index="
                + str(i)
            )

        return {"success": True, "data": data}

    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": "解析超时。该平台当前可能要求额外验证。",
        }
    except Exception as e:
        return {"success": False, "error": str(e)[:1800]}


@app.get("/api/download")
async def download_video(
    source: str = Query(...),
    index: int = Query(0, ge=0, le=30),
):
    try:
        validate_source_url(source)
        data = await parse_any(source)
        videos = data.get("videos") or []

        if index >= len(videos):
            raise HTTPException(status_code=404, detail="视频清晰度不存在")

        media_url = videos[index]["url"]
        if not is_public_remote_url(media_url):
            raise HTTPException(status_code=400, detail="媒体地址无效")

        headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Referer": data.get("referer") or data.get("resolved_url") or source,
        }

        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30, read=None),
            headers=headers,
        )
        request = client.build_request("GET", media_url)
        response = await client.send(request, stream=True)

        if response.status_code >= 400:
            await response.aclose()
            await client.aclose()
            raise HTTPException(
                status_code=502,
                detail=f"视频源返回 HTTP {response.status_code}",
            )

        title = re.sub(r'[\\/:*?"<>|]+', "_", data.get("title") or "video")[:80]
        ext = videos[index].get("ext") or "mp4"
        filename = f"{title}.{ext}"

        async def body_iter():
            try:
                async for chunk in response.aiter_bytes(1024 * 256):
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        content_type = response.headers.get("content-type", "application/octet-stream")
        headers_out = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        }

        return StreamingResponse(
            body_iter(),
            media_type=content_type,
            headers=headers_out,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:1000])
