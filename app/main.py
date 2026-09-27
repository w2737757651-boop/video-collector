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

app = FastAPI(title="Video Collector", version="3.0")
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
