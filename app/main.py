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
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Video Collector", version="3.2")
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
    "mp.weixin.qq.com",
    "channels.weixin.qq.com",
    "finder.video.qq.com",
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

PRIVATE_NETS = ("127.", "10.", "192.168.", "169.254.", "0.", "224.", "240.")


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
        try:
            ip = socket.gethostbyname(host)
            if (
                ip.startswith("127.")
                or ip.startswith("10.")
                or ip.startswith("192.168.")
                or ip.startswith("169.254.")
                or (ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31)
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
                raw = re.sub(r"\bundefined\b", "null", text[start:i + 1])
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
                or ("video" in low and "xhs" in low)
            )
        elif platform == "douyin":
            likely = (
                ".mp4" in low
                or "douyinvod" in low
                or "bytev" in low
                or ("video" in low and ("douyin" in low or "snssdk" in low))
            )
        elif platform == "wechat":
            likely = (
                "finder.video.qq.com" in low
                or ".mp4" in low
                or ".m3u8" in low
                or ("video" in low and "qq.com" in low)
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
    if (
        "weixin.qq.com" in host
        or "channels.weixin.qq.com" in host
        or "finder.video.qq.com" in host
    ):
        return "wechat"
    return "generic"


async def resolve_public_share(url: str) -> str:
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



def _xhs_is_video_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    url = normalize_url(url).strip()
    if not url.startswith(("http://", "https://")):
        return False
    low = url.lower()
    blocked_ext = ('.jpg','.jpeg','.png','.webp','.gif','.avif','.bmp','.svg')
    if any(ext in low for ext in blocked_ext):
        return False
    blocked_tokens = ('imageview','imageprocess','thumbnail','thumb','cover','avatar','logo','banner','advert','adsystem','/ads/','spectrum/')
    if any(token in low for token in blocked_tokens):
        return False
    positive_tokens = ('.mp4','.m3u8','sns-video','sns_video','video-stream','video_stream','/video/')
    return any(token in low for token in positive_tokens)


def collect_xhs_video_urls(state):
    results=[]
    seen=set()
    def add(url, quality=None, width=None, height=None):
        if not isinstance(url,str): return
        url=normalize_url(url)
        if not _xhs_is_video_url(url): return
        if not is_public_remote_url(url): return
        if url in seen: return
        seen.add(url)
        low=url.lower()
        ext='m3u8' if '.m3u8' in low else 'mp4'
        results.append({'quality':quality or (f'{height}p' if height else '视频源'),'url':url,'ext':ext,'width':width,'height':height})
    def walk(obj,path=''):
        if isinstance(obj,dict):
            lower_path=path.lower()
            if any(t in lower_path for t in ('advert','advertise','ads','recommend','sponsor','banner','commercial')):
                return
            width=obj.get('width') if isinstance(obj.get('width'),int) else None
            height=obj.get('height') if isinstance(obj.get('height'),int) else None
            video_context=any(t in lower_path for t in ('video','stream','media','master','h264','h265','hevc'))
            for key,value in obj.items():
                key_l=str(key).lower()
                child_path=f'{path}.{key_l}' if path else key_l
                if any(t in key_l for t in ('image','cover','poster','avatar','thumbnail','thumb')):
                    continue
                if isinstance(value,str):
                    if video_context or any(t in key_l for t in ('masterurl','playurl','play_url','streamurl','stream_url','videourl','video_url')):
                        add(value,width=width,height=height)
                elif isinstance(value,list):
                    if video_context and any(t in key_l for t in ('url','play','stream','master')):
                        for item in value:
                            if isinstance(item,str): add(item,width=width,height=height)
                            else: walk(item,child_path)
                    else:
                        for item in value: walk(item,child_path)
                elif isinstance(value,dict):
                    walk(value,child_path)
        elif isinstance(obj,list):
            for idx,item in enumerate(obj):
                walk(item,f'{path}[{idx}]')
    walk(state)
    results.sort(key=lambda x:((x.get('height') or 0),(x.get('width') or 0)),reverse=True)
    return results



def _xhs_note_id_from_url(url: str):
    if not isinstance(url, str):
        return None

    patterns = (
        r"/explore/([0-9a-fA-F]{16,32})",
        r"/discovery/item/([0-9a-fA-F]{16,32})",
        r"/item/([0-9a-fA-F]{16,32})",
    )

    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)

    return None


async def verify_video_resource(url: str, referer: str | None = None) -> bool:
    """
    Verify that a candidate URL actually returns video/HLS bytes,
    not an image, HTML page, redirect ad, or unrelated asset.
    """
    if not is_public_remote_url(url):
        return False

    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Range": "bytes=0-4095",
    }

    if referer:
        headers["Referer"] = referer

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15,
            headers=headers,
            http2=True,
        ) as client:
            r = await client.get(url)

        if r.status_code not in (200, 206):
            return False

        content_type = (r.headers.get("content-type") or "").lower()
        body = r.content[:4096]
        low_body = body.lower()

        # Strong rejections
        if content_type.startswith("image/"):
            return False

        if "text/html" in content_type:
            return False

        if body.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF87a", b"GIF89a", b"RIFF")):
            # RIFF may be WEBP; reject here for XHS video validation.
            return False

        # Strong video/HLS positives
        if content_type.startswith("video/"):
            return True

        if "mpegurl" in content_type or "application/vnd.apple.mpegurl" in content_type:
            return True

        if b"#EXTM3U" in body[:256]:
            return True

        # MP4 signature: "ftyp" generally appears very early in file.
        if b"ftyp" in body[:128]:
            return True

        # Some XHS CDN nodes return generic octet-stream for MP4.
        if "application/octet-stream" in content_type and b"ftyp" in body[:512]:
            return True

        return False

    except Exception:
        return False


def _xhs_unwrap(value):
    """
    Xiaohongshu SSR data may wrap reactive values in:
      {"_value": ...}
      {"value": ...}
    Unwrap a few layers safely.
    """
    current = value

    for _ in range(6):
        if not isinstance(current, dict):
            break

        if "_value" in current and len(current) <= 4:
            current = current.get("_value")
            continue

        if "value" in current and len(current) <= 4:
            current = current.get("value")
            continue

        break

    return current


def _xhs_find_note_detail_map(state):
    """
    Search the entire initial state recursively for noteDetailMap
    instead of assuming a fixed top-level path.
    """
    visited = set()

    def walk(obj):
        obj = _xhs_unwrap(obj)

        obj_id = id(obj)
        if obj_id in visited:
            return None
        visited.add(obj_id)

        if isinstance(obj, dict):
            if "noteDetailMap" in obj:
                candidate = _xhs_unwrap(obj.get("noteDetailMap"))
                if isinstance(candidate, dict) and candidate:
                    return candidate

            for value in obj.values():
                found = walk(value)
                if found is not None:
                    return found

        elif isinstance(obj, list):
            for value in obj:
                found = walk(value)
                if found is not None:
                    return found

        return None

    return walk(state)


def _xhs_find_note_container(state, target_note_id=None):
    """
    Locate the exact note from any noteDetailMap found in SSR state.
    Handles reactive wrappers such as _value/value.
    """
    detail_map = _xhs_find_note_detail_map(state)

    if not isinstance(detail_map, dict) or not detail_map:
        return None, None

    # Exact map key first.
    if target_note_id:
        direct = _xhs_unwrap(detail_map.get(target_note_id))

        if isinstance(direct, dict):
            note = _xhs_unwrap(direct.get("note"))

            if isinstance(note, dict):
                return str(target_note_id), note

    valid = []

    for map_key, container in detail_map.items():
        container = _xhs_unwrap(container)

        if not isinstance(container, dict):
            continue

        note = _xhs_unwrap(container.get("note"))

        # Some payloads may store the note object directly.
        if not isinstance(note, dict):
            maybe_note = _xhs_unwrap(container)

            if isinstance(maybe_note, dict) and (
                "video" in maybe_note
                or "noteId" in maybe_note
                or "id" in maybe_note
            ):
                note = maybe_note

        if not isinstance(note, dict):
            continue

        nid = str(
            note.get("noteId")
            or note.get("id")
            or map_key
            or ""
        )

        valid.append((str(map_key), note, nid))

        if target_note_id and nid == str(target_note_id):
            return str(map_key), note

    # If only one note was returned, use it.
    if len(valid) == 1:
        map_key, note, _ = valid[0]
        return map_key, note

    # Prefer an explicit video note among multiple records.
    for map_key, note, _ in valid:
        note_type = str(note.get("type") or "").lower()

        if note_type == "video" and isinstance(
            _xhs_unwrap(note.get("video")),
            dict
        ):
            return map_key, note

    if valid:
        map_key, note, _ = valid[0]
        return map_key, note

    return None, None



def extract_xhs_structured_video(state, target_note_id=None):
    """
    Extract Xiaohongshu video streams from the note's structured video node.

    Preferred path:
      note.video.media.stream.h264[*].masterUrl

    Falls back to h265/hevc only if h264 is unavailable.
    """
    note_id, note = _xhs_find_note_container(state, target_note_id)

    if not note:
        raise ValueError("页面存在 __INITIAL_STATE__，但递归搜索后仍未找到 noteDetailMap。")

    note_type = str(note.get("type") or "").lower()

    # Some responses may omit explicit type but still contain video data.
    video = _xhs_unwrap(note.get("video"))
    if not isinstance(video, dict):
        if note_type and note_type != "video":
            raise ValueError("当前小红书笔记不是视频笔记。")
        raise ValueError("小红书笔记存在，但没有找到 video 节点。")

    media = _xhs_unwrap(video.get("media"))
    if not isinstance(media, dict):
        raise ValueError("找到小红书 video 节点，但缺少 media 数据。")

    stream = _xhs_unwrap(media.get("stream"))
    if not isinstance(stream, dict):
        raise ValueError("找到小红书 video.media，但缺少 stream 数据。")

    candidates = []

    # Prefer H264 for widest playback/download compatibility.
    for codec_key in ("h264", "h265", "hevc"):
        items = _xhs_unwrap(stream.get(codec_key))
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue

            url = (
                item.get("masterUrl")
                or item.get("master_url")
                or item.get("url")
                or item.get("playUrl")
                or item.get("play_url")
            )

            if not isinstance(url, str) or not url.strip():
                continue

            url = normalize_url(url.strip())

            if not is_public_remote_url(url):
                continue

            low = url.lower()
            if any(ext in low for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
                continue

            width = item.get("width")
            height = item.get("height")

            quality = (
                f"{height}p"
                if isinstance(height, int) and height > 0
                else str(item.get("qualityType") or item.get("quality_type") or codec_key.upper())
            )

            candidates.append({
                "quality": quality,
                "url": url,
                "ext": "m3u8" if ".m3u8" in low else "mp4",
                "width": width if isinstance(width, int) else None,
                "height": height if isinstance(height, int) else None,
                "codec": codec_key,
            })

        if candidates and codec_key == "h264":
            break

    if not candidates:
        raise ValueError("这是视频笔记，但没有找到可访问的视频流 masterUrl。")

    # Dedupe by URL
    deduped = []
    seen = set()
    for item in candidates:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        deduped.append(item)

    deduped.sort(
        key=lambda x: ((x.get("height") or 0), (x.get("width") or 0)),
        reverse=True,
    )

    title = (
        note.get("title")
        or note.get("displayTitle")
        or note.get("desc")
        or "小红书视频"
    )

    user = note.get("user") if isinstance(note.get("user"), dict) else {}
    author = (
        user.get("nickname")
        or user.get("nickName")
        or user.get("name")
    )

    cover = None
    image_list = note.get("imageList")
    if isinstance(image_list, list) and image_list:
        first_img = image_list[0]
        if isinstance(first_img, dict):
            cover = (
                first_img.get("urlDefault")
                or first_img.get("urlPre")
                or first_img.get("url")
            )

    # Some video nodes provide cover separately.
    if not cover:
        cover_obj = video.get("cover")
        if isinstance(cover_obj, dict):
            cover = (
                cover_obj.get("urlDefault")
                or cover_obj.get("url")
                or cover_obj.get("urlPre")
            )
        elif isinstance(cover_obj, str):
            cover = cover_obj

    duration = (
        video.get("duration")
        or media.get("duration")
    )

    return {
        "note_id": note_id,
        "title": title,
        "author": author,
        "cover": normalize_url(cover or "") or None,
        "duration": duration,
        "videos": deduped[:12],
    }



def _xhs_has_security_context(url: str) -> bool:
    try:
        p = urlparse(url)
        q = parse_qs(p.query, keep_blank_values=True)
        return bool(q.get("xsec_token")) and bool(q.get("xsec_source"))
    except Exception:
        return False


def _xhs_extract_redirect_path(url: str):
    try:
        p = urlparse(url)
        q = parse_qs(p.query, keep_blank_values=True)
        raw = q.get("redirectPath", [None])[0]
        if not raw:
            return None
        candidate = unquote(raw)
        if candidate.startswith("http"):
            return candidate
    except Exception:
        pass
    return None


async def resolve_xhs_share_context(source_url: str) -> str:
    """
    Resolve an official XHS short link while preserving the tokenized
    public note URL from any redirect hop.

    Priority:
      1. Any redirect/final URL containing xsec_token + xsec_source
      2. login?redirectPath=<tokenized public note URL>
      3. final public URL as last resort
    """
    headers = {
        **BASE_HEADERS,
        "Referer": "https://www.xiaohongshu.com/",
    }

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=20,
        headers=headers,
        http2=True,
    ) as client:
        r = await client.get(source_url)

    chain = []

    for hist in r.history:
        # Request URL for each hop
        try:
            chain.append(str(hist.request.url))
        except Exception:
            pass

        # Location target if present
        loc = hist.headers.get("location")
        if loc:
            try:
                from urllib.parse import urljoin
                chain.append(urljoin(str(hist.request.url), loc))
            except Exception:
                chain.append(loc)

    chain.append(str(r.url))

    # First preserve any explicit tokenized public note URL.
    for candidate in chain:
        if _xhs_has_security_context(candidate):
            return candidate

        redirected = _xhs_extract_redirect_path(candidate)
        if redirected and _xhs_has_security_context(redirected):
            return redirected

    # A login page can still contain the exact public target in redirectPath.
    for candidate in chain:
        redirected = _xhs_extract_redirect_path(candidate)
        if redirected:
            return redirected

    return str(r.url)



async def parse_xhs_with_browser(source_url: str):
    """
    Browser-rendered fallback for public Xiaohongshu notes.
    It does not inject login cookies or bypass CAPTCHA/login.
    It only reads data/resources exposed to a normal anonymous browser page.
    """
    captured = []
    seen = set()

    def maybe_add(url: str):
        if not isinstance(url, str):
            return

        url = normalize_url(url)

        if not url.startswith(("http://", "https://")):
            return

        low = url.lower()

        # Only video-like XHS CDN resources.
        if not (
            "sns-video" in low
            or ".mp4" in low
            or ".m3u8" in low
        ):
            return

        # Reject obvious images/static assets.
        if any(ext in low for ext in (
            ".jpg", ".jpeg", ".png", ".webp",
            ".gif", ".avif", ".svg"
        )):
            return

        if url in seen:
            return

        seen.add(url)
        captured.append(url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = await browser.new_context(
            user_agent=UA,
            locale="zh-CN",
            viewport={"width": 1280, "height": 900},
        )

        page = await context.new_page()

        page.on(
            "request",
            lambda req: maybe_add(req.url)
        )

        try:
            await page.goto(
                source_url,
                wait_until="domcontentloaded",
                timeout=45000,
            )

            # Let client-side hydration/network requests complete.
            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=12000,
                )
            except Exception:
                pass

            await page.wait_for_timeout(2500)

            final_url = page.url

            low_final = final_url.lower()

            if (
                "/login" in low_final
                or "captcha" in low_final
                or "/404" in low_final
            ):
                raise ValueError(
                    "小红书浏览器访问被跳转到登录、验证码或限制页面。"
                    "本工具不会绕过验证。"
                )

            # Read hydrated state directly from browser JS context.
            state = await page.evaluate(
                """() => {
                    try {
                        return window.__INITIAL_STATE__ || null;
                    } catch (e) {
                        return null;
                    }
                }"""
            )

            target_note_id = (
                _xhs_note_id_from_url(final_url)
                or _xhs_note_id_from_url(source_url)
            )

            structured = None

            if isinstance(state, dict):
                try:
                    structured = extract_xhs_structured_video(
                        state,
                        target_note_id=target_note_id,
                    )
                except Exception:
                    structured = None

            # DOM <video> is a useful fallback after hydration.
            dom_video_urls = await page.evaluate(
                """() => Array.from(document.querySelectorAll('video'))
                    .map(v => v.currentSrc || v.src || '')
                    .filter(Boolean)"""
            )

            for u in dom_video_urls or []:
                maybe_add(u)

            # Also inspect <source> children.
            dom_source_urls = await page.evaluate(
                """() => Array.from(document.querySelectorAll('video source'))
                    .map(s => s.src || '')
                    .filter(Boolean)"""
            )

            for u in dom_source_urls or []:
                maybe_add(u)

            candidates = []

            if structured:
                candidates.extend(
                    structured.get("videos") or []
                )

            for url in captured:
                if all(
                    item.get("url") != url
                    for item in candidates
                ):
                    candidates.append({
                        "quality": "浏览器视频源",
                        "url": url,
                        "ext": (
                            "m3u8"
                            if ".m3u8" in url.lower()
                            else "mp4"
                        ),
                        "width": None,
                        "height": None,
                    })

            verified = []

            for item in candidates:
                url = item.get("url")

                if not url:
                    continue

                if await verify_video_resource(
                    url,
                    referer=final_url,
                ):
                    verified.append(item)

            if not verified:
                raise ValueError(
                    "浏览器已正常打开小红书页面，但没有捕获到可验证的视频流。"
                    "该笔记可能未向匿名网页端暴露视频地址，或当前 Render IP 被限流。"
                )

            title = None
            author = None
            cover = None
            duration = None
            note_id = target_note_id

            if structured:
                title = structured.get("title")
                author = structured.get("author")
                cover = structured.get("cover")
                duration = structured.get("duration")
                note_id = structured.get("note_id") or note_id

            if not title:
                try:
                    title = await page.title()
                except Exception:
                    title = None

            return {
                "platform": "XiaoHongShu",
                "id": note_id,
                "title": title or "小红书视频",
                "author": author,
                "cover": cover,
                "duration": duration,
                "source_url": source_url,
                "resolved_url": final_url,
                "referer": final_url,
                "videos": verified[:12],
                "resolver": "browser",
            }

        finally:
            await context.close()
            await browser.close()


async def parse_xiaohongshu(source_url: str):
    """
    Public-page parser:
      1) Try lightweight HTTP SSR.
      2) If SSR is simplified/missing note detail, fall back to a real browser.
    """
    http_error = None

    try:
        direct_url = await resolve_xhs_share_context(source_url)

        final_url, page = await fetch_html(
            direct_url,
            referer="https://www.xiaohongshu.com/",
        )

        if (
            "/login" in final_url
            or "captcha" in final_url.lower()
        ):
            raise ValueError(
                "小红书 HTTP 页面被跳转到登录/验证码页面。"
            )

        state = (
            extract_balanced_json(
                page,
                "window.__INITIAL_STATE__"
            )
            or extract_balanced_json(
                page,
                "__INITIAL_STATE__"
            )
        )

        if not state:
            raise ValueError(
                "HTTP 页面没有完整 __INITIAL_STATE__。"
            )

        target_note_id = (
            _xhs_note_id_from_url(final_url)
            or _xhs_note_id_from_url(direct_url)
            or _xhs_note_id_from_url(source_url)
        )

        structured = extract_xhs_structured_video(
            state,
            target_note_id=target_note_id,
        )

        verified = []

        for item in structured.get("videos") or []:
            url = item.get("url")

            if (
                url
                and await verify_video_resource(
                    url,
                    referer=final_url,
                )
            ):
                verified.append(item)

        if not verified:
            raise ValueError(
                "HTTP SSR 找到视频结构，但视频流校验失败。"
            )

        return {
            "platform": "XiaoHongShu",
            "id": structured.get("note_id"),
            "title": structured.get("title") or "小红书视频",
            "author": structured.get("author"),
            "cover": structured.get("cover"),
            "duration": structured.get("duration"),
            "source_url": source_url,
            "resolved_url": final_url,
            "referer": final_url,
            "videos": verified[:12],
            "resolver": "http_ssr",
        }

    except Exception as exc:
        http_error = str(exc)

    # Real browser fallback for simplified SSR / JS-hydrated pages.
    try:
        return await parse_xhs_with_browser(source_url)

    except Exception as browser_error:
        raise ValueError(
            "小红书 HTTP 解析失败："
            + str(http_error)
            + "；浏览器解析失败："
            + str(browser_error)
        )


def extract_douyin_aweme_id(url: str) -> str | None:
    patterns = (
        r"/video/(\d+)",
        r"/share/video/(\d+)",
        r"[?&]modal_id=(\d+)",
        r"[?&]aweme_id=(\d+)",
    )
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def find_douyin_item(state):
    if not isinstance(state, (dict, list)):
        return None

    # Common SSR shape:
    # loaderData -> <page> -> videoInfoRes -> item_list[0]
    if isinstance(state, dict):
        loader = state.get("loaderData")
        if isinstance(loader, dict):
            for page_data in loader.values():
                if isinstance(page_data, dict):
                    info = page_data.get("videoInfoRes")
                    if isinstance(info, dict):
                        items = info.get("item_list") or info.get("itemList")
                        if isinstance(items, list) and items and isinstance(items[0], dict):
                            return items[0]

    # Fallback: look for a dict that has a video object and description.
    for node in iter_nodes(state):
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("video"), dict) and (
            node.get("desc") or node.get("aweme_id") or node.get("awemeId")
        ):
            return node

    return None



def extract_douyin_audio(item: dict):
    if not isinstance(item, dict):
        return []

    music = item.get("music")
    if not isinstance(music, dict):
        return []

    play = music.get("play_url") or music.get("playUrl")
    urls = []

    if isinstance(play, dict):
        urls = play.get("url_list") or play.get("urlList") or play.get("urls") or []
    elif isinstance(play, list):
        urls = play

    title = (
        music.get("title")
        or music.get("music_name")
        or music.get("musicName")
        or "抖音音频"
    )
    author = (
        music.get("author")
        or music.get("owner_nickname")
        or music.get("ownerNickname")
    )

    result = []
    seen = set()

    for url in urls:
        if not isinstance(url, str):
            continue
        url = normalize_url(url)
        if not url.startswith(("http://", "https://")):
            continue
        if url in seen or not is_public_remote_url(url):
            continue
        seen.add(url)

        low = url.lower()
        if ".m4a" in low:
            ext = "m4a"
        elif ".aac" in low:
            ext = "aac"
        elif ".mp3" in low:
            ext = "mp3"
        else:
            ext = "mp3"

        result.append({
            "quality": "原声音频",
            "url": url,
            "ext": ext,
            "abr": None,
            "title": title,
            "author": author,
        })

    return result


def douyin_item_to_result(item: dict, source_url: str, resolved_url: str):
    video = item.get("video") or {}

    urls = []
    seen = set()

    def add_urls(value, quality=None):
        candidates = []
        if isinstance(value, dict):
            candidates = (
                value.get("url_list")
                or value.get("urlList")
                or value.get("urls")
                or []
            )
        elif isinstance(value, list):
            candidates = value

        for u in candidates:
            if not isinstance(u, str):
                continue
            u = normalize_url(u)
            if not u.startswith(("http://", "https://")) or u in seen:
                continue
            if not is_public_remote_url(u):
                continue
            seen.add(u)
            urls.append({
                "quality": quality or "公开视频源",
                "url": u,
                "ext": "mp4",
                "width": video.get("width"),
                "height": video.get("height"),
            })

    # Prefer the standard public playback address from the SSR payload.
    add_urls(video.get("play_addr") or video.get("playAddr"), "公开播放源")

    # Additional public variants, if present.
    bit_rate = video.get("bit_rate") or video.get("bitRate") or []
    if isinstance(bit_rate, list):
        for br in bit_rate:
            if not isinstance(br, dict):
                continue
            label = (
                br.get("gear_name")
                or br.get("gearName")
                or br.get("quality_type")
                or "视频源"
            )
            add_urls(br.get("play_addr") or br.get("playAddr"), str(label))

    if not urls:
        # Generic fallback against the item only.
        urls = collect_media_urls(item, "douyin")

    if not urls:
        raise ValueError("已读取抖音公开 SSR 数据，但没有找到公开播放地址。")

    author_obj = item.get("author") if isinstance(item.get("author"), dict) else {}
    stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}

    cover = None
    for key in ("cover", "origin_cover", "originCover", "dynamic_cover", "dynamicCover"):
        obj = video.get(key)
        if isinstance(obj, dict):
            lst = obj.get("url_list") or obj.get("urlList") or []
            if isinstance(lst, list) and lst:
                cover = normalize_url(lst[0])
                break

    duration = video.get("duration") or item.get("duration")
    if isinstance(duration, (int, float)) and duration > 10000:
        duration = round(duration / 1000, 3)

    return {
        "platform": "Douyin",
        "id": str(item.get("aweme_id") or item.get("awemeId") or ""),
        "title": item.get("desc") or "抖音视频",
        "author": (
            author_obj.get("nickname")
            or author_obj.get("unique_id")
            or author_obj.get("uniqueId")
        ),
        "cover": cover,
        "duration": duration,
        "source_url": source_url,
        "resolved_url": resolved_url,
        "referer": resolved_url,
        "videos": urls[:12],
        "audios": extract_douyin_audio(item)[:8],
    }



async def fetch_douyin_mobile_feed_item(aweme_id: str):
    """
    Query Douyin's mobile feed endpoints for a public video ID.
    No account cookies, CAPTCHA solving, or access-control bypass is used.
    """
    endpoints = (
        "https://api5-normal-c-hl.amemv.com/aweme/v1/feed/",
        "https://aweme.snssdk.com/aweme/v1/feed/",
    )

    headers = {
        "User-Agent": (
            "com.ss.android.ugc.aweme/280500 "
            "(Linux; U; Android 13; zh_CN; Pixel 7; Build/TQ3A.230805.001)"
        ),
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    params = {
        "aweme_id": aweme_id,
        "aid": "1128",
    }

    last_error = None

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=20,
        headers=headers,
        http2=True,
    ) as client:
        for endpoint in endpoints:
            try:
                r = await client.get(endpoint, params=params)
                if r.status_code >= 400:
                    last_error = f"{endpoint} HTTP {r.status_code}"
                    continue

                data = r.json()

                candidates = []
                if isinstance(data, dict):
                    for key in ("aweme_list", "item_list", "awemeList", "itemList"):
                        value = data.get(key)
                        if isinstance(value, list):
                            candidates.extend(value)

                for item in candidates:
                    if not isinstance(item, dict):
                        continue
                    iid = str(item.get("aweme_id") or item.get("awemeId") or "")
                    if iid == aweme_id:
                        return item

                # Some responses contain only one result without an exact id match.
                for item in candidates:
                    if isinstance(item, dict) and isinstance(item.get("video"), dict):
                        return item

                last_error = f"{endpoint} 返回成功但未找到作品"
            except Exception as e:
                last_error = str(e)

    if last_error:
        raise ValueError("抖音移动端 Feed 未返回视频数据：" + last_error)
    raise ValueError("抖音移动端 Feed 未返回视频数据")


async def parse_douyin(source_url: str):
    # Resolve share URL and get public video id.
    resolved = await resolve_public_share(source_url)
    aweme_id = extract_douyin_aweme_id(resolved) or extract_douyin_aweme_id(source_url)

    # 1) Main path: mobile Feed API for ordinary public videos.
    if aweme_id:
        try:
            item = await fetch_douyin_mobile_feed_item(aweme_id)
            return douyin_item_to_result(
                item,
                source_url=source_url,
                resolved_url=resolved,
            )
        except Exception:
            pass

    # 2) Fallback: public mobile SSR share page.
    if aweme_id:
        ssr_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/?from_ssr=1"
        try:
            ssr_final, ssr_page = await fetch_html(
                ssr_url,
                referer="https://www.douyin.com/",
            )

            state = (
                extract_balanced_json(ssr_page, "window._ROUTER_DATA")
                or extract_balanced_json(ssr_page, "_ROUTER_DATA")
                or extract_balanced_json(ssr_page, "__INITIAL_STATE__")
            )

            if state:
                item = find_douyin_item(state)
                if item:
                    return douyin_item_to_result(
                        item,
                        source_url=source_url,
                        resolved_url=ssr_final,
                    )
        except Exception:
            pass

    # 3) Last public-page fallback.
    final_url, page = await fetch_html(resolved)

    if "/login" in final_url or "captcha" in final_url.lower():
        raise ValueError("抖音公开页面当前要求登录或验证码，未尝试绕过访问限制。")

    state = (
        extract_balanced_json(page, "window._ROUTER_DATA")
        or extract_balanced_json(page, "_ROUTER_DATA")
        or extract_balanced_json(page, "__INITIAL_STATE__")
        or extract_balanced_json(page, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
    )

    if not state:
        m = re.search(r'id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>', page, re.S | re.I)
        if m:
            try:
                raw = unquote(html.unescape(m.group(1)))
                state = json.loads(raw)
            except Exception:
                state = None

    if state:
        item = find_douyin_item(state)
        if item:
            return douyin_item_to_result(
                item,
                source_url=source_url,
                resolved_url=final_url,
            )

        videos = collect_media_urls(state, "douyin")
        if videos:
            title = first_string_for_keys(state, ("desc", "title", "description"))
            author = first_string_for_keys(state, ("nickname", "uniqueId", "unique_id", "name"))
            cover = first_string_for_keys(state, ("cover", "coverUrl", "originCover", "dynamicCover"))
            duration = first_number_for_keys(state, ("duration",))
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

    raise ValueError(
        "抖音公开 Feed、SSR 和普通公开页面均未返回可直接访问的视频资源。"
        "该作品当前可能受到地区、内容类型或平台访问策略限制。"
    )



def extract_wechat_direct_urls_from_html(page: str):
    """
    Extract media URLs already exposed by the public share page.
    No login/session credentials are injected.
    """
    candidates = []
    seen = set()

    # Normalize common escaping styles first.
    normalized = (
        html.unescape(page)
        .replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )

    patterns = (
        r'https?://finder\.video\.qq\.com/[^\s"\'<>]+',
        r'https?://[^\s"\'<>]+\.video\.qq\.com/[^\s"\'<>]+',
        r'https?://[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?',
        r'https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?',
    )

    for pattern in patterns:
        for m in re.finditer(pattern, normalized, re.I):
            url = normalize_url(m.group(0)).rstrip('",);]')
            if not url.startswith(("http://", "https://")):
                continue
            if not is_public_remote_url(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            candidates.append({
                "quality": "公开视频源",
                "url": url,
                "ext": "m3u8" if ".m3u8" in url.lower() else "mp4",
                "width": None,
                "height": None,
            })

    return candidates



WECHAT_THIRD_PARTY_RESOLVER = (
    "https://sph.litao.workers.dev/api/fetch_video_profile"
)


async def parse_wechat_via_third_party(source_url: str):
    """
    Third-party fallback for WeChat Channels share links.

    Only the share URL is sent to the resolver.
    No local WeChat cookies, passwords, or login session are sent.
    """
    payload = {"url": source_url}

    headers = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30,
        headers=headers,
        http2=True,
    ) as client:
        response = await client.post(
            WECHAT_THIRD_PARTY_RESOLVER,
            json=payload,
        )

        if response.status_code >= 400:
            raise ValueError(
                "视频号第三方解析服务返回 HTTP "
                + str(response.status_code)
            )

        try:
            data = response.json()
        except Exception:
            raise ValueError(
                "视频号第三方解析服务返回了非 JSON 数据。"
            )

    if not isinstance(data, dict):
        raise ValueError(
            "视频号第三方解析服务返回格式异常。"
        )

    err_code = data.get("errCode")
    err_msg = data.get("errMsg")

    if err_code not in (None, 0, "0"):
        raise ValueError(
            "视频号第三方解析失败："
            + str(err_msg or err_code)
        )

    root = data.get("data")
    if not isinstance(root, dict):
        root = {}

    feed_info = root.get("feedInfo")
    if not isinstance(feed_info, dict):
        feed_info = {}

    author_info = root.get("authorInfo")
    if not isinstance(author_info, dict):
        author_info = {}

    video_url = (
        feed_info.get("videoUrl")
        or feed_info.get("video_url")
        or feed_info.get("url")
    )

    if not isinstance(video_url, str) or not video_url.strip():
        raise ValueError(
            "视频号第三方解析服务没有返回可下载的视频地址。"
        )

    video_url = normalize_url(video_url.strip())

    if not is_public_remote_url(video_url):
        raise ValueError(
            "视频号第三方解析服务返回的视频地址无效。"
        )

    title = (
        feed_info.get("description")
        or feed_info.get("desc")
        or feed_info.get("title")
        or "视频号视频"
    )

    author = (
        author_info.get("nickname")
        or author_info.get("name")
        or author_info.get("username")
        or feed_info.get("nickname")
    )

    cover = (
        feed_info.get("coverUrl")
        or feed_info.get("cover")
        or feed_info.get("thumbUrl")
        or feed_info.get("poster")
    )

    duration = (
        feed_info.get("duration")
        or feed_info.get("videoDuration")
    )

    video_id = (
        feed_info.get("objectId")
        or feed_info.get("feedId")
        or feed_info.get("id")
    )

    return {
        "platform": "WeChatChannels",
        "id": str(video_id) if video_id is not None else None,
        "title": str(title),
        "author": str(author) if author else None,
        "cover": normalize_url(cover or "") or None,
        "duration": duration,
        "source_url": source_url,
        "resolved_url": source_url,
        "referer": "https://weixin.qq.com/",
        "videos": [
            {
                "quality": "视频号直链",
                "url": video_url,
                "ext": (
                    "m3u8"
                    if ".m3u8" in video_url.lower()
                    else "mp4"
                ),
                "width": None,
                "height": None,
            }
        ],
        "resolver": "third_party",
    }


async def parse_wechat_channels(source_url: str):
    """
    Public-page-only WeChat Channels parser.

    Supports public share pages such as:
      - https://weixin.qq.com/sph/...
      - https://mp.weixin.qq.com/sph/...
      - channels.weixin.qq.com public pages

    If the public page does not expose a media URL, return a clear authorization
    boundary instead of using account cookies or bypassing login.
    """
    final_url, page = await fetch_html(
        source_url,
        referer="https://weixin.qq.com/",
    )

    low_final = final_url.lower()
    low_page = page.lower()

    if (
        "/login" in low_final
        or "captcha" in low_final
        or "verify" in low_final
        or "请在微信客户端打开" in page
    ):
        raise ValueError(
            "视频号公开页面当前要求微信客户端、登录或验证；未尝试绕过访问限制。"
        )

    # 1) Direct media URLs exposed in the page source.
    videos = extract_wechat_direct_urls_from_html(page)

    # 2) Common embedded state blobs.
    states = []
    for marker_name in (
        "window.__INITIAL_STATE__",
        "__INITIAL_STATE__",
        "__NEXT_DATA__",
        "window.cgiData",
        "cgiData",
        "feedInfo",
    ):
        state = extract_balanced_json(page, marker_name)
        if state:
            states.append(state)

    for state in states:
        for item in collect_media_urls(state, "wechat"):
            if all(item["url"] != x["url"] for x in videos):
                videos.append(item)

    if not videos:
        raise ValueError(
            "已读取视频号公开分享页面，但页面没有暴露可直接访问的视频资源。"
            "当前该链接可能需要微信登录/授权会话；本工具不会注入或绕过账号凭证。"
        )

    merged_state = states[0] if states else {}

    title = first_string_for_keys(
        merged_state,
        ("title", "desc", "description", "feedDesc", "nickname"),
    ) if merged_state else None

    author = first_string_for_keys(
        merged_state,
        ("nickname", "authorName", "finderUsername", "username", "name"),
    ) if merged_state else None

    cover = first_string_for_keys(
        merged_state,
        ("cover", "coverUrl", "thumbUrl", "imageUrl", "poster"),
    ) if merged_state else None

    duration = first_number_for_keys(
        merged_state,
        ("duration", "videoDuration"),
    ) if merged_state else None

    return {
        "platform": "WeChatChannels",
        "id": (
            first_string_for_keys(
                merged_state,
                ("objectId", "feedId", "id"),
            )
            if merged_state else None
        ),
        "title": title or "视频号视频",
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
    audios = []

    for f in info.get("formats") or []:
        media_url = f.get("url")
        if not media_url:
            continue

        if not is_public_remote_url(media_url):
            continue

        vcodec = f.get("vcodec")
        acodec = f.get("acodec")

        # Audio-only format
        if vcodec == "none" and acodec not in (None, "none"):
            audios.append({
                "quality": (
                    f"{round(f.get('abr'))} kbps"
                    if isinstance(f.get("abr"), (int, float))
                    else f.get("format_note")
                    or f.get("format_id")
                    or "音频"
                ),
                "url": media_url,
                "ext": f.get("ext") or "m4a",
                "abr": f.get("abr"),
                "title": info.get("title"),
                "author": (
                    info.get("uploader")
                    or info.get("channel")
                    or info.get("creator")
                ),
            })
            continue

        # Video format
        if vcodec == "none":
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
    audios.sort(
        key=lambda x: x.get("abr") or 0,
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
        "audios": audios[:8],
    }


async def parse_any(source_url: str):
    validate_source_url(source_url)
    platform = platform_from_url(source_url)

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

    if platform == "wechat":
        public_error = None

        try:
            return await parse_wechat_channels(source_url)
        except Exception as exc:
            public_error = str(exc)

        try:
            return await parse_wechat_via_third_party(source_url)
        except Exception as third_party_error:
            try:
                return await parse_with_ytdlp(source_url)
            except Exception:
                raise ValueError(
                    "视频号公开页面解析失败："
                    + str(public_error)
                    + "；第三方解析失败："
                    + str(third_party_error)
                )

    return await parse_with_ytdlp(source_url)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"ok": True, "version": "3.2"}


@app.post("/api/parse")
async def parse_video(body: ParseBody):
    try:
        source_url = extract_url(body.text)
        data = await parse_any(source_url)

        for i, item in enumerate(data.get("videos") or []):
            item["download_url"] = (
                "/api/download?source="
                + quote(source_url, safe="")
                + "&kind=video&index="
                + str(i)
            )

        for i, item in enumerate(data.get("audios") or []):
            item["download_url"] = (
                "/api/download?source="
                + quote(source_url, safe="")
                + "&kind=audio&index="
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
    kind: str = Query("video"),
    index: int = Query(0, ge=0, le=30),
):
    try:
        validate_source_url(source)
        data = await parse_any(source)

        if kind not in ("video", "audio"):
            raise HTTPException(status_code=400, detail="kind 只支持 video 或 audio")

        media_items = (
            data.get("audios")
            if kind == "audio"
            else data.get("videos")
        ) or []

        if index >= len(media_items):
            raise HTTPException(
                status_code=404,
                detail="音频不存在" if kind == "audio" else "视频清晰度不存在",
            )

        media_url = media_items[index]["url"]

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
                detail=f"媒体源返回 HTTP {response.status_code}",
            )

        title = re.sub(r'[\\/:*?"<>|]+', "_", data.get("title") or "media")[:80]
        ext = media_items[index].get("ext") or ("m4a" if kind == "audio" else "mp4")
        suffix = "_音频" if kind == "audio" else ""
        filename = f"{title}{suffix}.{ext}"

        async def body_iter():
            try:
                async for chunk in response.aiter_bytes(1024 * 256):
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        content_type = response.headers.get(
            "content-type",
            "application/octet-stream"
        )
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

