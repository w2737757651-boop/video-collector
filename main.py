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
    return {"ok": True, "version": "2.7"}


@app.post("/api/parse")
async def parse_video(body: ParseBody):
    try:
        source_url = extract_url(body.text)
        data = await parse_any(source_url)

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
