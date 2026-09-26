import asyncio
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from yt_dlp import YoutubeDL


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Video Collector", version="1.0")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

ALLOWED_HOST_PARTS = (
    "douyin.com",
    "xiaohongshu.com",
    "xhslink.com",
    "xhslink.cn",
    "kuaishou.com",
    "bilibili.com",
    "b23.tv",
    "weixin.qq.com",
)

URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)


class ParseBody(BaseModel):
    text: str


def extract_url(text: str) -> str:
    match = URL_RE.search((text or "").strip())
    if not match:
        raise ValueError("没有识别到有效链接")
    return match.group(0).rstrip("，。,.；;）)")


def validate_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("只支持 http/https 链接")
    host = (p.hostname or "").lower()
    if not any(part in host for part in ALLOWED_HOST_PARTS):
        raise ValueError("当前测试版只支持抖音、小红书、快手、B站和微信相关分享链接")


def extract_sync(url: str):
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


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/parse")
async def parse_video(body: ParseBody):
    try:
        url = extract_url(body.text)
        validate_url(url)

        info = await asyncio.wait_for(
            asyncio.to_thread(extract_sync, url),
            timeout=45
        )

        if not info:
            return {"success": False, "error": "解析器没有返回视频数据"}

        formats = []
        for f in info.get("formats") or []:
            media_url = f.get("url")
            if not media_url:
                continue
            if f.get("vcodec") == "none":
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
            reverse=True
        )

        return {
            "success": True,
            "data": {
                "platform": info.get("extractor_key") or info.get("extractor"),
                "id": info.get("id"),
                "title": info.get("title"),
                "author": info.get("uploader") or info.get("channel") or info.get("creator"),
                "cover": info.get("thumbnail"),
                "duration": info.get("duration"),
                "source_url": url,
                "videos": formats[:8],
            }
        }

    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": "解析超时。该平台当前可能需要登录、验证码或额外验证。"
        }
    except Exception as e:
        msg = str(e)
        if "cookies" in msg.lower():
            msg += "；该链接当前可能需要有效登录状态。"
        return {"success": False, "error": msg[:1800]}
