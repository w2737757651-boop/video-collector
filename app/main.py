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

app = FastAPI(
    title="Video Collector",
    version="2.3"
)

app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static"
)


URL_RE = re.compile(
    r'https?://[^\s<>"\']+',
    re.I
)


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
    "Mozilla/5.0 "
    "(Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/126.0.0.0 "
    "Mobile Safari/537.36"
)


BASE_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Accept": (
        "text/html,"
        "application/xhtml+xml,"
        "application/xml;q=0.9,"
        "*/*;q=0.8"
    ),
}


PRIVATE_NETS = (
    "127.",
    "10.",
    "192.168.",
    "169.254.",
    "0.",
    "224.",
    "240.",
)


class ParseBody(BaseModel):
    text: str


# =========================================================
# 基础工具
# =========================================================

def extract_url(text: str) -> str:
    match = URL_RE.search(
        (text or "").strip()
    )

    if not match:
        raise ValueError(
            "没有识别到有效链接"
        )

    return match.group(0).rstrip(
        "，。,.；;）)"
    )


def source_host_allowed(
    url: str
) -> bool:

    try:
        host = (
            urlparse(url).hostname
            or ""
        ).lower()

    except Exception:
        return False

    return any(
        host == domain
        or host.endswith("." + domain)

        for domain
        in ALLOWED_SOURCE_HOSTS
    )


def validate_source_url(
    url: str
) -> None:

    parsed = urlparse(url)

    if parsed.scheme not in (
        "http",
        "https"
    ):
        raise ValueError(
            "只支持 http/https 链接"
        )

    if not source_host_allowed(url):
        raise ValueError(
            "当前版本只支持抖音、小红书、快手、B站和微信相关分享链接"
        )


def normalize_url(
    value: str
) -> str:

    if not isinstance(
        value,
        str
    ):
        return ""

    value = (
        html.unescape(value)
        .replace("\\u002F", "/")
        .replace("\\/", "/")
    )

    if value.startswith("//"):
        value = "https:" + value

    return value


def is_public_remote_url(
    url: str
) -> bool:

    try:

        parsed = urlparse(url)

        if (
            parsed.scheme not in (
                "http",
                "https"
            )
            or not parsed.hostname
        ):
            return False

        host = parsed.hostname.lower()

        if (
            host == "localhost"
            or any(
                host.startswith(prefix)
                for prefix
                in PRIVATE_NETS
            )
        ):
            return False

        try:

            ip = socket.gethostbyname(
                host
            )

            if (
                ip.startswith("127.")
                or ip.startswith("10.")
                or ip.startswith("192.168.")
                or ip.startswith("169.254.")
            ):
                return False

            if ip.startswith("172."):

                second = int(
                    ip.split(".")[1]
                )

                if 16 <= second <= 31:
                    return False

        except Exception:
            pass

        return True

    except Exception:
        return False


async def fetch_html(
    url: str,
    referer: str | None = None
):

    headers = dict(
        BASE_HEADERS
    )

    if referer:
        headers["Referer"] = referer

    async with httpx.AsyncClient(

        follow_redirects=True,

        timeout=20,

        headers=headers,

        http2=True,

    ) as client:

        response = await client.get(
            url
        )

        response.raise_for_status()

        return (
            str(response.url),
            response.text
        )


# =========================================================
# JSON / 页面数据提取
# =========================================================

def extract_balanced_json(
    text: str,
    marker: str
):

    position = text.find(
        marker
    )

    if position < 0:
        return None

    start_object = text.find(
        "{",
        position + len(marker)
    )

    start_array = text.find(
        "[",
        position + len(marker)
    )

    candidates = [

        value

        for value
        in (
            start_object,
            start_array
        )

        if value >= 0

    ]

    if not candidates:
        return None

    start = min(
        candidates
    )

    opening = text[start]

    closing = (
        "}"
        if opening == "{"
        else "]"
    )

    depth = 0

    in_string = False

    escape = False

    for index in range(
        start,
        len(text)
    ):

        char = text[index]

        if in_string:

            if escape:
                escape = False

            elif char == "\\":
                escape = True

            elif char == '"':
                in_string = False

            continue

        if char == '"':
            in_string = True

        elif char == opening:
            depth += 1

        elif char == closing:

            depth -= 1

            if depth == 0:

                raw = text[
                    start:index + 1
                ]

                raw = re.sub(
                    r"\bundefined\b",
                    "null",
                    raw
                )

                try:
                    return json.loads(
                        raw
                    )

                except Exception:
                    return None

    return None


def iter_nodes(obj):

    if isinstance(
        obj,
        dict
    ):

        yield obj

        for value in obj.values():

            yield from iter_nodes(
                value
            )

    elif isinstance(
        obj,
        list
    ):

        for value in obj:

            yield from iter_nodes(
                value
            )


def first_string_for_keys(
    obj,
    keys
):

    for node in iter_nodes(
        obj
    ):

        for key in keys:

            value = node.get(
                key
            )

            if (
                isinstance(
                    value,
                    str
                )
                and value.strip()
            ):

                return value.strip()

    return None


def first_number_for_keys(
    obj,
    keys
):

    for node in iter_nodes(
        obj
    ):

        for key in keys:

            value = node.get(
                key
            )

            if isinstance(
                value,
                (int, float)
            ):

                return value

    return None


# =========================================================
# 通用媒体地址查找
# =========================================================

def collect_media_urls(
    obj,
    platform: str
):

    found = []

    seen = set()

    def add(
        url,
        width=None,
        height=None,
        quality=None
    ):

        url = normalize_url(
            url
        )

        if not url:
            return

        if not url.startswith(
            (
                "http://",
                "https://"
            )
        ):
            return

        low = url.lower()

        if platform == "xiaohongshu":

            likely = (
                "xhscdn.com" in low
                or "sns-video" in low
                or ".mp4" in low
                or (
                    "video" in low
                    and "xhs" in low
                )
            )

        elif platform == "douyin":

            likely = (
                ".mp4" in low
                or "douyinvod" in low
                or "bytev" in low
                or (
                    "video" in low
                    and (
                        "douyin" in low
                        or "snssdk" in low
                    )
                )
            )

        elif platform == "wechat":

            likely = (
                "finder.video.qq.com"
                in low

                or ".mp4"
                in low

                or ".m3u8"
                in low

                or (
                    "video" in low
                    and "qq.com" in low
                )
            )

        else:

            likely = (
                ".mp4" in low
                or ".m3u8" in low
            )

        if not likely:
            return

        if url in seen:
            return

        seen.add(url)

        if not is_public_remote_url(
            url
        ):
            return

        if (
            quality is None
            and height
        ):

            quality = (
                f"{height}p"
            )

        found.append({

            "quality":
                quality
                or "视频源",

            "url":
                url,

            "ext":
                (
                    "m3u8"
                    if ".m3u8" in low
                    else "mp4"
                ),

            "width":
                width,

            "height":
                height,
        })

    for node in iter_nodes(
        obj
    ):

        width = (
            node.get("width")
            if isinstance(
                node.get("width"),
                int
            )
            else None
        )

        height = (
            node.get("height")
            if isinstance(
                node.get("height"),
                int
            )
            else None
        )

        for key, value in node.items():

            key_lower = str(
                key
            ).lower()

            if isinstance(
                value,
                str
            ):

                if any(

                    token
                    in key_lower

                    for token
                    in (
                        "url",
                        "src",
                        "play",
                        "video",
                        "stream",
                        "master",
                    )

                ):

                    add(
                        value,
                        width,
                        height
                    )

            elif (
                isinstance(
                    value,
                    list
                )
                and any(

                    token
                    in key_lower

                    for token
                    in (
                        "url",
                        "play",
                        "video",
                        "stream",
                        "master",
                    )

                )
            ):

                for item in value:

                    if isinstance(
                        item,
                        str
                    ):

                        add(
                            item,
                            width,
                            height
                        )

    found.sort(

        key=lambda item: (

            item.get("height")
            or 0,

            item.get("width")
            or 0,

        ),

        reverse=True,
    )

    return found


# =========================================================
# 平台识别
# =========================================================

def platform_from_url(
    url: str
) -> str:

    host = (
        urlparse(url).hostname
        or ""
    ).lower()

    if (
        "douyin" in host
        or "iesdouyin" in host
    ):
        return "douyin"

    if (
        "xiaohongshu" in host
        or "xhslink" in host
    ):
        return "xiaohongshu"

    if (
        "bilibili" in host
        or "b23.tv" in host
    ):
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


# =========================================================
# 分享短链解析
# =========================================================

async def resolve_public_share(
    url: str
) -> str:

    headers = dict(
        BASE_HEADERS
    )

    async with httpx.AsyncClient(

        follow_redirects=True,

        timeout=20,

        headers=headers,

    ) as client:

        response = await client.get(
            url
        )

        final_url = str(
            response.url
        )

    parsed = urlparse(
        final_url
    )

    if (
        "xiaohongshu.com"
        in (
            parsed.hostname
            or ""
        )

        and parsed.path.startswith(
            "/login"
        )
    ):

        query = parse_qs(
            parsed.query
        )

        redirect_path = query.get(
            "redirectPath",
            [None]
        )[0]

        if redirect_path:

            candidate = unquote(
                redirect_path
            )

            if (
                candidate.startswith(
                    "http"
                )

                and source_host_allowed(
                    candidate
                )
            ):

                return candidate

    return final_url


# =========================================================
# 小红书
# =========================================================

async def parse_xiaohongshu(
    source_url: str
):

    direct_url = await resolve_public_share(
        source_url
    )

    final_url, page = await fetch_html(
        direct_url
    )

    if (
        "/login" in final_url
        or "captcha"
        in final_url.lower()
    ):

        raise ValueError(
            "小红书公开页面当前要求登录或验证码，未尝试绕过访问限制。"
        )

    state = (

        extract_balanced_json(
            page,
            "window.__INITIAL_STATE__"
        )

        or

        extract_balanced_json(
            page,
            "__INITIAL_STATE__"
        )
    )

    if not state:

        raise ValueError(
            "小红书页面已打开，但没有找到公开的 __INITIAL_STATE__ 数据。"
        )

    videos = collect_media_urls(
        state,
        "xiaohongshu"
    )

    if not videos:

        raise ValueError(
            "已读取小红书公开页面，但没有找到可直接访问的视频资源。"
        )

    title = first_string_for_keys(

        state,

        (
            "title",
            "displayTitle",
            "desc",
            "description",
        )
    )

    author = first_string_for_keys(

        state,

        (
            "nickname",
            "nickName",
            "name",
        )
    )

    cover = first_string_for_keys(

        state,

        (
            "cover",
            "coverUrl",
            "image",
            "imageUrl",
        )
    )

    duration = first_number_for_keys(

        state,

        (
            "duration",
            "videoDuration",
        )
    )

    return {

        "platform":
            "XiaoHongShu",

        "id":
            first_string_for_keys(
                state,
                (
                    "noteId",
                    "id",
                )
            ),

        "title":
            title
            or "小红书视频",

        "author":
            author,

        "cover":
            (
                normalize_url(
                    cover or ""
                )
                or None
            ),

        "duration":
            duration,

        "source_url":
            source_url,

        "resolved_url":
            final_url,

        "referer":
            final_url,

        "videos":
            videos[:12],
    }


# =========================================================
# 抖音
# =========================================================

def extract_douyin_aweme_id(
    url: str
):

    patterns = (

        r"/video/(\d+)",

        r"/share/video/(\d+)",

        r"[?&]modal_id=(\d+)",

        r"[?&]aweme_id=(\d+)",
    )

    for pattern in patterns:

        match = re.search(
            pattern,
            url
        )

        if match:
            return match.group(1)

    return None


def find_douyin_item(
    state
):

    if not isinstance(
        state,
        (dict, list)
    ):
        return None

    if isinstance(
        state,
        dict
    ):

        loader = state.get(
            "loaderData"
        )

        if isinstance(
            loader,
            dict
        ):

            for page_data in loader.values():

                if not isinstance(
                    page_data,
                    dict
                ):
                    continue

                info = page_data.get(
                    "videoInfoRes"
                )

                if not isinstance(
                    info,
                    dict
                ):
                    continue

                items = (
                    info.get("item_list")
                    or info.get("itemList")
                )

                if (
                    isinstance(
                        items,
                        list
                    )

                    and items

                    and isinstance(
                        items[0],
                        dict
                    )
                ):

                    return items[0]

    for node in iter_nodes(
        state
    ):

        if not isinstance(
            node,
            dict
        ):
            continue

        if (
            isinstance(
                node.get("video"),
                dict
            )

            and (
                node.get("desc")
                or node.get("aweme_id")
                or node.get("awemeId")
            )
        ):

            return node

    return None


def douyin_item_to_result(
    item: dict,
    source_url: str,
    resolved_url: str
):

    video = (
        item.get("video")
        or {}
    )

    urls = []

    seen = set()

    def add_urls(
        value,
        quality=None
    ):

        candidates = []

        if isinstance(
            value,
            dict
        ):

            candidates = (

                value.get("url_list")

                or value.get("urlList")

                or value.get("urls")

                or []
            )

        elif isinstance(
            value,
            list
        ):

            candidates = value

        for url in candidates:

            if not isinstance(
                url,
                str
            ):
                continue

            url = normalize_url(
                url
            )

            if not url.startswith(
                (
                    "http://",
                    "https://"
                )
            ):
                continue

            if url in seen:
                continue

            if not is_public_remote_url(
                url
            ):
                continue

            seen.add(
                url
            )

            urls.append({

                "quality":
                    quality
                    or "公开视频源",

                "url":
                    url,

                "ext":
                    "mp4",

                "width":
                    video.get("width"),

                "height":
                    video.get("height"),
            })

    add_urls(

        video.get("play_addr")
        or video.get("playAddr"),

        "公开播放源",
    )

    bit_rate = (

        video.get("bit_rate")

        or video.get("bitRate")

        or []
    )

    if isinstance(
        bit_rate,
        list
    ):

        for bit_rate_item in bit_rate:

            if not isinstance(
                bit_rate_item,
                dict
            ):
                continue

            label = (

                bit_rate_item.get(
                    "gear_name"
                )

                or bit_rate_item.get(
                    "gearName"
                )

                or bit_rate_item.get(
                    "quality_type"
                )

                or "视频源"
            )

            add_urls(

                bit_rate_item.get(
                    "play_addr"
                )

                or bit_rate_item.get(
                    "playAddr"
                ),

                str(label),
            )

    if not urls:

        urls = collect_media_urls(
            item,
            "douyin"
        )

    if not urls:

        raise ValueError(
            "已读取抖音公开数据，但没有找到公开播放地址。"
        )

    author_object = (

        item.get("author")

        if isinstance(
            item.get("author"),
            dict
        )

        else {}
    )

    cover = None

    for key in (
        "cover",
        "origin_cover",
        "originCover",
        "dynamic_cover",
        "dynamicCover",
    ):

        value = video.get(
            key
        )

        if not isinstance(
            value,
            dict
        ):
            continue

        url_list = (

            value.get("url_list")

            or value.get("urlList")

            or []
        )

        if (
            isinstance(
                url_list,
                list
            )

            and url_list
        ):

            cover = normalize_url(
                url_list[0]
            )

            break

    duration = (

        video.get("duration")

        or item.get("duration")
    )

    if (
        isinstance(
            duration,
            (int, float)
        )

        and duration > 10000
    ):

        duration = round(
            duration / 1000,
            3
        )

    return {

        "platform":
            "Douyin",

        "id":
            str(
                item.get("aweme_id")
                or item.get("awemeId")
                or ""
            ),

        "title":
            item.get("desc")
            or "抖音视频",

        "author":
            (
                author_object.get(
                    "nickname"
                )

                or author_object.get(
                    "unique_id"
                )

                or author_object.get(
                    "uniqueId"
                )
            ),

        "cover":
            cover,

        "duration":
            duration,

        "source_url":
            source_url,

        "resolved_url":
            resolved_url,

        "referer":
            resolved_url,

        "videos":
            urls[:12],
    }


async def fetch_douyin_mobile_feed_item(
    aweme_id: str
):

    endpoints = (

        "https://api5-normal-c-hl.amemv.com/aweme/v1/feed/",

        "https://aweme.snssdk.com/aweme/v1/feed/",
    )

    headers = {

        "User-Agent": (
            "com.ss.android.ugc.aweme/280500 "
            "(Linux; U; Android 13; zh_CN; "
            "Pixel 7; Build/TQ3A.230805.001)"
        ),

        "Accept":
            "application/json",

        "Accept-Language":
            "zh-CN,zh;q=0.9",
    }

    params = {

        "aweme_id":
            aweme_id,

        "aid":
            "1128",
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

                response = await client.get(

                    endpoint,

                    params=params
                )

                if (
                    response.status_code
                    >= 400
                ):

                    last_error = (
                        f"{endpoint} "
                        f"HTTP "
                        f"{response.status_code}"
                    )

                    continue

                data = response.json()

                candidates = []

                if isinstance(
                    data,
                    dict
                ):

                    for key in (

                        "aweme_list",

                        "item_list",

                        "awemeList",

                        "itemList",
                    ):

                        value = data.get(
                            key
                        )

                        if isinstance(
                            value,
                            list
                        ):

                            candidates.extend(
                                value
                            )

                for item in candidates:

                    if not isinstance(
                        item,
                        dict
                    ):
                        continue

                    item_id = str(

                        item.get(
                            "aweme_id"
                        )

                        or item.get(
                            "awemeId"
                        )

                        or ""
                    )

                    if item_id == aweme_id:
                        return item

                for item in candidates:

                    if (
                        isinstance(
                            item,
                            dict
                        )

                        and isinstance(
                            item.get("video"),
                            dict
                        )
                    ):

                        return item

                last_error = (
                    f"{endpoint} "
                    "返回成功但未找到作品"
                )

            except Exception as exc:

                last_error = str(
                    exc
                )

    if last_error:

        raise ValueError(
            "抖音移动端 Feed 未返回视频数据："
            + last_error
        )

    raise ValueError(
        "抖音移动端 Feed 未返回视频数据"
    )


async def parse_douyin(
    source_url: str
):

    resolved = await resolve_public_share(
        source_url
    )

    aweme_id = (

        extract_douyin_aweme_id(
            resolved
        )

        or

        extract_douyin_aweme_id(
            source_url
        )
    )

    # 1. 移动端 Feed

    if aweme_id:

        try:

            item = await fetch_douyin_mobile_feed_item(
                aweme_id
            )

            return douyin_item_to_result(

                item,

                source_url=source_url,

                resolved_url=resolved,
            )

        except Exception:
            pass

    # 2. SSR

    if aweme_id:

        ssr_url = (
            "https://www.iesdouyin.com/"
            f"share/video/{aweme_id}/"
            "?from_ssr=1"
        )

        try:

            ssr_final, ssr_page = await fetch_html(

                ssr_url,

                referer=(
                    "https://www.douyin.com/"
                ),
            )

            state = (

                extract_balanced_json(
                    ssr_page,
                    "window._ROUTER_DATA"
                )

                or

                extract_balanced_json(
                    ssr_page,
                    "_ROUTER_DATA"
                )

                or

                extract_balanced_json(
                    ssr_page,
                    "__INITIAL_STATE__"
                )
            )

            if state:

                item = find_douyin_item(
                    state
                )

                if item:

                    return douyin_item_to_result(

                        item,

                        source_url=source_url,

                        resolved_url=ssr_final,
                    )

        except Exception:
            pass

    # 3. 普通公开页面

    final_url, page = await fetch_html(
        resolved
    )

    if (
        "/login" in final_url
        or "captcha"
        in final_url.lower()
    ):

        raise ValueError(
            "抖音公开页面当前要求登录或验证码，未尝试绕过访问限制。"
        )

    state = (

        extract_balanced_json(
            page,
            "window._ROUTER_DATA"
        )

        or

        extract_balanced_json(
            page,
            "_ROUTER_DATA"
        )

        or

        extract_balanced_json(
            page,
            "__INITIAL_STATE__"
        )

        or

        extract_balanced_json(
            page,
            "__UNIVERSAL_DATA_FOR_REHYDRATION__"
        )
    )

    if not state:

        match = re.search(

            r'id=["\']RENDER_DATA["\']'
            r'[^>]*>'
            r'(.*?)'
            r'</script>',

            page,

            re.S | re.I,
        )

        if match:

            try:

                raw = unquote(

                    html.unescape(
                        match.group(1)
                    )
                )

                state = json.loads(
                    raw
                )

            except Exception:
                state = None

    if state:

        item = find_douyin_item(
            state
        )

        if item:

            return douyin_item_to_result(

                item,

                source_url=source_url,

                resolved_url=final_url,
            )

        videos = collect_media_urls(
            state,
            "douyin"
        )

        if videos:

            title = first_string_for_keys(

                state,

                (
                    "desc",
                    "title",
                    "description",
                )
            )

            author = first_string_for_keys(

                state,

                (
                    "nickname",
                    "uniqueId",
                    "unique_id",
                    "name",
                )
            )

            cover = first_string_for_keys(

                state,

                (
                    "cover",
                    "coverUrl",
                    "originCover",
                    "dynamicCover",
                )
            )

            duration = first_number_for_keys(

                state,

                ("duration",)
            )

            if (
                isinstance(
                    duration,
                    (int, float)
                )
                and duration > 10000
            ):

                duration = round(
                    duration / 1000,
                    3
                )

            return {

                "platform":
                    "Douyin",

                "id":
                    first_string_for_keys(

                        state,

                        (
                            "awemeId",
                            "aweme_id",
                            "id",
                        )
                    ),

                "title":
                    title
                    or "抖音视频",

                "author":
                    author,

                "cover":
                    (
                        normalize_url(
                            cover or ""
                        )
                        or None
                    ),

                "duration":
                    duration,

                "source_url":
                    source_url,

                "resolved_url":
                    final_url,

                "referer":
                    final_url,

                "videos":
                    videos[:12],
            }

    raise ValueError(
        "抖音公开 Feed、SSR 和普通公开页面均未返回可直接访问的视频资源。"
        "该作品当前可能受到地区、内容类型或平台访问策略限制。"
    )


# =========================================================
# 视频号
# =========================================================

def extract_wechat_direct_urls_from_html(
    page: str
):

    candidates = []

    seen = set()

    normalized = (

        html.unescape(page)

        .replace(
            "\\u002F",
            "/"
        )

        .replace(
            "\\/",
            "/"
        )

        .replace(
            "&amp;",
            "&"
        )
    )

    patterns = (

        r'https?://finder\.video\.qq\.com/[^\s"\'<>]+',

        r'https?://[^\s"\'<>]+\.video\.qq\.com/[^\s"\'<>]+',

        r'https?://[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?',

        r'https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?',
    )

    for pattern in patterns:

        for match in re.finditer(

            pattern,

            normalized,

            re.I,
        ):

            url = normalize_url(
                match.group(0)
            ).rstrip(
                '",);]'
            )

            if not url.startswith(
                (
                    "http://",
                    "https://"
                )
            ):
                continue

            if not is_public_remote_url(
                url
            ):
                continue

            if url in seen:
                continue

            seen.add(
                url
            )

            candidates.append({

                "quality":
                    "公开视频源",

                "url":
                    url,

                "ext":
                    (
                        "m3u8"
                        if ".m3u8"
                        in url.lower()

                        else "mp4"
                    ),

                "width":
                    None,

                "height":
                    None,
            })

    return candidates


async def parse_wechat_channels(
    source_url: str
):

    final_url, page = await fetch_html(

        source_url,

        referer=(
            "https://weixin.qq.com/"
        ),
    )

    low_final = final_url.lower()

    if (
        "/login" in low_final
        or "captcha" in low_final
        or "verify" in low_final
        or "请在微信客户端打开"
        in page
    ):

        raise ValueError(
            "视频号公开页面当前要求微信客户端、登录或验证；未尝试绕过访问限制。"
        )

    videos = (
        extract_wechat_direct_urls_from_html(
            page
        )
    )

    states = []

    for marker in (

        "window.__INITIAL_STATE__",

        "__INITIAL_STATE__",

        "__NEXT_DATA__",

        "window.cgiData",

        "cgiData",

        "feedInfo",
    ):

        state = extract_balanced_json(
            page,
            marker
        )

        if state:
            states.append(
                state
            )

    for state in states:

        extra_videos = collect_media_urls(
            state,
            "wechat"
        )

        for item in extra_videos:

            if all(

                item["url"]
                != existing["url"]

                for existing
                in videos

            ):

                videos.append(
                    item
                )

    if not videos:

        raise ValueError(
            "已读取视频号公开分享页面，但页面没有暴露可直接访问的视频资源。"
            "当前该链接可能需要微信登录/授权会话；"
            "本工具不会注入或绕过账号凭证。"
        )

    merged_state = (
        states[0]
        if states
        else {}
    )

    title = (

        first_string_for_keys(

            merged_state,

            (
                "title",
                "desc",
                "description",
                "feedDesc",
            )
        )

        if merged_state

        else None
    )

    author = (

        first_string_for_keys(

            merged_state,

            (
                "nickname",
                "authorName",
                "finderUsername",
                "username",
                "name",
            )
        )

        if merged_state

        else None
    )

    cover = (

        first_string_for_keys(

            merged_state,

            (
                "cover",
                "coverUrl",
                "thumbUrl",
                "imageUrl",
                "poster",
            )
        )

        if merged_state

        else None
    )

    duration = (

        first_number_for_keys(

            merged_state,

            (
                "duration",
                "videoDuration",
            )
        )

        if merged_state

        else None
    )

    video_id = (

        first_string_for_keys(

            merged_state,

            (
                "objectId",
                "feedId",
                "id",
            )
        )

        if merged_state

        else None
    )

    return {

        "platform":
            "WeChatChannels",

        "id":
            video_id,

        "title":
            title
            or "视频号视频",

        "author":
            author,

        "cover":
            (
                normalize_url(
                    cover or ""
                )
                or None
            ),

        "duration":
            duration,

        "source_url":
            source_url,

        "resolved_url":
            final_url,

        "referer":
            final_url,

        "videos":
            videos[:12],
    }


# =========================================================
# yt-dlp 通用解析
# =========================================================

def ytdlp_extract_sync(
    url: str
):

    options = {

        "quiet":
            True,

        "no_warnings":
            True,

        "skip_download":
            True,

        "noplaylist":
            True,

        "extract_flat":
            False,

        "socket_timeout":
            20,

        "retries":
            1,

        "fragment_retries":
            1,
    }

    with YoutubeDL(
        options
    ) as ydl:

        return ydl.extract_info(

            url,

            download=False
        )


async def parse_with_ytdlp(
    url: str
):

    info = await asyncio.wait_for(

        asyncio.to_thread(

            ytdlp_extract_sync,

            url
        ),

        timeout=45,
    )

    if not info:

        raise ValueError(
            "解析器没有返回视频数据"
        )

    formats = []

    for item in (
        info.get("formats")
        or []
    ):

        media_url = item.get(
            "url"
        )

        if not media_url:
            continue

        if (
            item.get("vcodec")
            == "none"
        ):
            continue

        if not is_public_remote_url(
            media_url
        ):
            continue

        formats.append({

            "quality":
                (
                    f"{item.get('height')}p"

                    if item.get(
                        "height"
                    )

                    else (
                        item.get(
                            "format_note"
                        )

                        or item.get(
                            "format_id"
                        )
                    )
                ),

            "url":
                media_url,

            "ext":
                item.get("ext"),

            "width":
                item.get("width"),

            "height":
                item.get("height"),
        })

    formats.sort(

        key=lambda item: (

            item.get("height")
            or 0,

            item.get("width")
            or 0,
        ),

        reverse=True,
    )

    return {

        "platform":
            (
                info.get(
                    "extractor_key"
                )

                or info.get(
                    "extractor"
                )
            ),

        "id":
            info.get("id"),

        "title":
            info.get("title"),

        "author":
            (
                info.get(
                    "uploader"
                )

                or info.get(
                    "channel"
                )

                or info.get(
                    "creator"
                )
            ),

        "cover":
            info.get(
                "thumbnail"
            ),

        "duration":
            info.get(
                "duration"
            ),

        "source_url":
            url,

        "resolved_url":
            url,

        "referer":
            url,

        "videos":
            formats[:12],
    }


# =========================================================
# 统一平台调度
# =========================================================

async def parse_any(
    source_url: str
):

    validate_source_url(
        source_url
    )

    platform = platform_from_url(
        source_url
    )

    # 抖音

    if platform == "douyin":

        try:

            return await parse_douyin(
                source_url
            )

        except Exception as adapter_error:

            try:

                return await parse_with_ytdlp(
                    source_url
                )

            except Exception:

                raise ValueError(
                    str(adapter_error)
                )

    # 小红书

    if platform == "xiaohongshu":

        try:

            return await parse_xiaohongshu(
                source_url
            )

        except Exception as adapter_error:

            try:

                return await parse_with_ytdlp(
                    source_url
                )

            except Exception:

                raise ValueError(
                    str(adapter_error)
                )

    # 视频号

    if platform == "wechat":

        try:

            return await parse_wechat_channels(
                source_url
            )

        except Exception as adapter_error:

            try:

                return await parse_with_ytdlp(
                    source_url
                )

            except Exception:

                raise ValueError(
                    str(adapter_error)
                )

    # B站 / 快手等

    return await parse_with_ytdlp(
        source_url
    )


# =========================================================
# 页面
# =========================================================

@app.get("/")
async def index():

    return FileResponse(
        STATIC_DIR
        / "index.html"
    )


@app.get("/health")
async def health():

    return {
        "ok": True,
        "version": "2.3"
    }


# =========================================================
# 解析 API
# =========================================================

@app.post("/api/parse")
async def parse_video(
    body: ParseBody
):

    try:

        source_url = extract_url(
            body.text
        )

        data = await parse_any(
            source_url
        )

        for index, item in enumerate(
            data.get("videos")
            or []
        ):

            item["download_url"] = (

                "/api/download?source="

                + quote(
                    source_url,
                    safe=""
                )

                + "&index="

                + str(index)
            )

        return {

            "success":
                True,

            "data":
                data
        }

    except asyncio.TimeoutError:

        return {

            "success":
                False,

            "error":
                "解析超时。该平台当前可能要求额外验证。"
        }

    except Exception as exc:

        return {

            "success":
                False,

            "error":
                str(exc)[:1800]
        }


# =========================================================
# 下载 API
# =========================================================

@app.get("/api/download")
async def download_video(

    source: str = Query(...),

    index: int = Query(
        0,
        ge=0,
        le=30
    ),
):

    try:

        validate_source_url(
            source
        )

        # 每次下载重新解析，
        # 避免旧 CDN 地址过期

        data = await parse_any(
            source
        )

        videos = (
            data.get("videos")
            or []
        )

        if index >= len(
            videos
        ):

            raise HTTPException(

                status_code=404,

                detail=(
                    "视频清晰度不存在"
                )
            )

        media_url = (
            videos[index]["url"]
        )

        if not is_public_remote_url(
            media_url
        ):

            raise HTTPException(

                status_code=400,

                detail=(
                    "媒体地址无效"
                )
            )

        headers = {

            "User-Agent":
                UA,

            "Accept":
                "*/*",

            "Referer":
                (
                    data.get(
                        "referer"
                    )

                    or data.get(
                        "resolved_url"
                    )

                    or source
                ),
        }

        client = httpx.AsyncClient(

            follow_redirects=True,

            timeout=httpx.Timeout(
                30,
                read=None
            ),

            headers=headers,
        )

        request = client.build_request(

            "GET",

            media_url
        )

        response = await client.send(

            request,

            stream=True
        )

        if (
            response.status_code
            >= 400
        ):

            await response.aclose()

            await client.aclose()

            raise HTTPException(

                status_code=502,

                detail=(
                    "视频源返回 HTTP "
                    f"{response.status_code}"
                )
            )

        title = re.sub(

            r'[\\/:*?"<>|]+',

            "_",

            data.get("title")
            or "video"

        )[:80]

        extension = (

            videos[index].get(
                "ext"
            )

            or "mp4"
        )

        filename = (
            f"{title}.{extension}"
        )

        async def body_iter():

            try:

                async for chunk in (
                    response.aiter_bytes(
                        1024 * 256
                    )
                ):

                    yield chunk

            finally:

                await response.aclose()

                await client.aclose()

        content_type = (

            response.headers.get(
                "content-type",
                "application/octet-stream"
            )
        )

        output_headers = {

            "Content-Disposition":

                "attachment; "
                "filename*=UTF-8''"
                + quote(filename)
        }

        return StreamingResponse(

            body_iter(),

            media_type=content_type,

            headers=output_headers,
        )

    except HTTPException:
        raise

    except Exception as exc:

        raise HTTPException(

            status_code=400,

            detail=str(exc)[:1000]
        )
