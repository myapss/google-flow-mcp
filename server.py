"""
Google Flow MCP server — wraps the useapi.net Google Flow API v1 so Claude.ai
can generate Veo 3.1 / Omni 1.1 Flash video and Nano Banana images.

Env vars:
  USEAPI_TOKEN     required  — your useapi.net token, e.g. "user:1234-abcd..."
  MCP_ACCESS_KEY   optional  — random secret; if set, the endpoint becomes
                               /<MCP_ACCESS_KEY>/mcp so nobody else can spend
                               your credits just by guessing the host.
  PORT             optional  — default 8000
  DEFAULT_EMAIL    optional  — pin every request to one Google Flow account
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Literal, Optional
from urllib.parse import quote

import base64
import json
import time
import uuid
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

BASE = "https://api.useapi.net/v1/google-flow"
TOKEN = os.environ.get("USEAPI_TOKEN", "").strip()
ACCESS_KEY = os.environ.get("MCP_ACCESS_KEY", "").strip()
DEFAULT_EMAIL = os.environ.get("DEFAULT_EMAIL", "").strip() or None
PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # e.g. https://xyz.up.railway.app
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "episodes").mkdir(exist_ok=True)
(DATA_DIR / "files").mkdir(exist_ok=True)

if not TOKEN:
    raise SystemExit("USEAPI_TOKEN is not set")

prefix = f"/{ACCESS_KEY}" if ACCESS_KEY else ""
http_path = f"{prefix}/mcp"

mcp = FastMCP(
    "Google Flow (useapi.net)",
    instructions=(
        "Generate images (Nano Banana 2 Lite / 2.1 / Pro) and videos (Veo 3.1, "
        "Omni 1.1 Flash) through the user's own Google Flow account via useapi.net. "
        "Images return in ~10-20s. Videos take 1-3 minutes: generate_video returns a "
        "jobId immediately; call wait_for_job or get_job to retrieve the result. "
        "Media URLs are signed and expire after ~6 hours."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path=http_path,
    stateless_http=True,
    json_response=True,
)

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _call(
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(
            method, f"{BASE}{path}", headers=HEADERS, json=json, params=params
        )
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    if r.status_code >= 400:
        retry = r.headers.get("Retry-After")
        return {
            "ok": False,
            "status": r.status_code,
            "retryAfterSeconds": int(retry) if retry and retry.isdigit() else None,
            "error": body,
        }
    return {"ok": True, "status": r.status_code, **(body if isinstance(body, dict) else {"data": body})}


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _file_url(name: str) -> str:
    return f"{PUBLIC_URL}{prefix}/files/{name}" if PUBLIC_URL else f"{prefix}/files/{name}"


async def _resolve_email(email: Optional[str]) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Return (email, error). Falls back to DEFAULT_EMAIL, then to the only connected account."""
    acct = email or DEFAULT_EMAIL
    if acct:
        return acct, None
    accounts = await _call("GET", "/accounts")
    if not accounts.get("ok"):
        return None, accounts
    emails = [k for k in accounts if "@" in k]
    if len(emails) == 1:
        return emails[0], None
    return None, {"ok": False, "error": "Specify `email` — more than one Google Flow account is connected.", "accounts": emails}


def _save_bytes(data: bytes, stem: str, ext: str) -> dict[str, Any]:
    name = f"{stem}.{ext}"
    (DATA_DIR / "files" / name).write_bytes(data)
    return {"file": name, "bytes": len(data), "downloadUrl": _file_url(name)}


def _summarize_images(resp: dict[str, Any]) -> dict[str, Any]:
    """Trim the verbose Google payload down to what Claude needs."""
    if not resp.get("ok"):
        return resp
    out = []
    for m in resp.get("media", []):
        g = (m.get("image") or {}).get("generatedImage") or {}
        out.append(
            _clean(
                {
                    "mediaGenerationId": g.get("mediaGenerationId"),
                    "url": g.get("fifeUrl"),
                    "seed": g.get("seed"),
                    "aspectRatio": g.get("aspectRatio"),
                    "hasInlineBase64": bool(g.get("encodedImage")),
                }
            )
        )
    return {"ok": True, "jobId": resp.get("jobId"), "images": out}


def _summarize_videos(media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in media or []:
        gv = (m.get("video") or {}).get("generatedVideo") or {}
        status = ((m.get("mediaMetadata") or {}).get("mediaStatus") or {}).get(
            "mediaGenerationStatus"
        )
        out.append(
            _clean(
                {
                    "mediaGenerationId": m.get("mediaGenerationId"),
                    "videoUrl": m.get("videoUrl"),
                    "thumbnailUrl": m.get("thumbnailUrl"),
                    "status": status,
                    "seed": gv.get("seed"),
                    "model": gv.get("model"),
                    "length": ((m.get("video") or {}).get("dimensions") or {}).get("length"),
                }
            )
        )
    return out


def _summarize_job(job: dict[str, Any]) -> dict[str, Any]:
    if not job.get("ok"):
        return job
    resp = job.get("response") or {}
    summary: dict[str, Any] = {
        "ok": True,
        "jobId": job.get("jobid"),
        "type": job.get("type"),
        "status": job.get("status"),
        "created": job.get("created"),
        "updated": job.get("updated"),
    }
    if job.get("status") == "failed":
        summary["error"] = job.get("error")
        summary["failureReasons"] = resp.get("failureReasons")
        return summary
    if job.get("type") == "video":
        summary["videos"] = _summarize_videos(resp.get("media", []))
        summary["remainingCredits"] = resp.get("remainingCredits")
    elif job.get("type") == "image":
        summary["images"] = _summarize_images({"ok": True, **resp})["images"]
    return summary


# --------------------------------------------------------------------------- #
# account tools
# --------------------------------------------------------------------------- #
@mcp.tool()
async def list_accounts() -> dict[str, Any]:
    """List the Google Flow accounts connected to this useapi.net subscription,
    with health and backend ('flow' = current, 'labs' = must be re-added)."""
    return await _call("GET", "/accounts")


@mcp.tool()
async def get_account(email: str) -> dict[str, Any]:
    """Get one account's health, remaining Flow credits, plan tier and the video
    models it can use (with per-model credit cost)."""
    r = await _call("GET", f"/accounts/{quote(email, safe='')}")
    if not r.get("ok"):
        return r
    models = [
        {
            "key": m.get("key"),
            "displayName": m.get("displayName"),
            "creditCost": m.get("creditCost"),
            "resolution": m.get("resolution"),
            "lengthSeconds": m.get("videoLengthSeconds"),
        }
        for m in ((r.get("models") or {}).get("videoModels") or [])
    ]
    return {
        "ok": True,
        "health": r.get("health"),
        "backend": r.get("backend"),
        "credits": r.get("credits"),
        "project": r.get("project"),
        "videoModels": models,
        "recommendations": r.get("recommendations"),
    }


@mcp.tool()
async def get_captcha_status() -> dict[str, Any]:
    """Show configured captcha providers, their balances, and any remaining
    free captcha credits. Generation needs captcha solves."""
    return await _call("GET", "/accounts/captcha-providers")


# --------------------------------------------------------------------------- #
# image tools
# --------------------------------------------------------------------------- #
@mcp.tool()
async def generate_image(
    prompt: str,
    model: Literal["nano-banana-2-lite", "nano-banana-2", "nano-banana-pro"] = "nano-banana-2-lite",
    aspect_ratio: Literal["16:9", "4:3", "1:1", "3:4", "9:16", "auto"] = "16:9",
    count: int = 1,
    seed: Optional[int] = None,
    reference_ids: Optional[list[str]] = None,
    character_ids: Optional[list[str]] = None,
    email: Optional[str] = None,
) -> dict[str, Any]:
    """Generate 1-4 images. Synchronous (~10-20 s). Works on any Google AI plan,
    including free.

    - model: nano-banana-2-lite (fast default), nano-banana-2 (2.1), nano-banana-pro (best).
    - reference_ids: up to 10 mediaGenerationIds (from upload_asset or a previous
      generation) to use as image references. 'auto' aspect ratio is only valid
      with references.
    - character_ids: up to 7 character refs. You may mention them in the prompt
      as @character_1, @reference_1 etc.
    Returns signed URLs valid ~6 h plus mediaGenerationIds for reuse.
    """
    body: dict[str, Any] = {
        "prompt": prompt,
        "model": model,
        "aspectRatio": aspect_ratio,
        "count": max(1, min(4, count)),
        "seed": seed,
        "email": email or DEFAULT_EMAIL,
    }
    for i, ref in enumerate(reference_ids or [], start=1):
        body[f"reference_{i}"] = ref
    for i, ch in enumerate(character_ids or [], start=1):
        body[f"character_{i}"] = ch
    r = await _call("POST", "/images", json=_clean(body), timeout=300)
    return _summarize_images(r)


@mcp.tool()
async def upscale_image(media_generation_id: str, resolution: Literal["2k", "4k"] = "2k") -> dict[str, Any]:
    """Upscale a generated image to 2k (any paid plan) or 4k (Ultra only). The JPEG is
    stored on this server and returned as a downloadUrl."""
    r = await _call("POST", "/images/upscale", json={"mediaGenerationId": media_generation_id, "resolution": resolution}, timeout=300)
    if not r.get("ok"):
        return r
    if not r.get("encodedImage"):
        return {"ok": False, "error": "No image returned"}
    return {"ok": True, **_save_bytes(base64.b64decode(r["encodedImage"]), f"upscale-{uuid.uuid4().hex[:8]}", "jpg")}


# --------------------------------------------------------------------------- #
# video tools
# --------------------------------------------------------------------------- #
@mcp.tool()
async def generate_video(
    prompt: str,
    model: Literal[
        "veo-3.1-fast", "veo-3.1-quality", "veo-3.1-lite", "veo-3.1-lite-low-priority", "omni-flash"
    ] = "veo-3.1-fast",
    aspect_ratio: Literal["landscape", "portrait", "1:1", "4:3", "3:4"] = "landscape",
    duration: Literal[4, 6, 8, 10] = 8,
    resolution: Literal["720p", "360p"] = "720p",
    count: int = 1,
    seed: Optional[int] = None,
    start_image_id: Optional[str] = None,
    end_image_id: Optional[str] = None,
    reference_image_ids: Optional[list[str]] = None,
    character_ids: Optional[list[str]] = None,
    voice_names: Optional[list[str]] = None,
    reference_video_id: Optional[str] = None,
    email: Optional[str] = None,
    wait: bool = False,
) -> dict[str, Any]:
    """Generate a video. Needs a paid Google AI plan (spends Flow credits).

    Submits asynchronously and returns a jobId right away. Set wait=true to
    block up to ~5 minutes and return the finished video URLs directly;
    otherwise call wait_for_job(jobId) next.

    Models / credit cost per clip:
      veo-3.1-lite 5-10 · veo-3.1-fast 10-20 (default) · veo-3.1-quality 100 (8 s only)
      veo-3.1-lite-low-priority 0 (Ultra $199 only) · omni-flash 4-15 (audio-native,
      4/6/8/10 s, 360p is ~half price; only model that supports 10 s and 360p).
    - start_image_id (+ optional end_image_id): image-to-video from uploaded/generated frames.
    - reference_image_ids: 'ingredients' mode, up to 3 on Veo (not quality), 7 on omni-flash.
      Cannot be combined with start/end images.
    - voice_names: up to 1 on Veo / 5 on omni-flash. System presets include Puck, Kore,
      Zephyr, Charon, Aoede, Fenrir, Leda, Orus… or a custom voice ref. Requires a
      reference image or character.
    - reference_video_id: omni-flash video-to-video edit of an uploaded/generated clip.
    """
    body: dict[str, Any] = {
        "prompt": prompt,
        "model": model,
        "aspectRatio": aspect_ratio,
        "duration": duration,
        "count": max(1, min(4, count)),
        "seed": seed,
        "email": email or DEFAULT_EMAIL,
        "startImage": start_image_id,
        "endImage": end_image_id,
        "referenceVideo_1": reference_video_id,
        "async": True,
    }
    if model == "omni-flash":
        body["resolution"] = resolution
    if reference_video_id:
        body.pop("duration", None)  # V2V edit length matches the input
    for i, ref in enumerate(reference_image_ids or [], start=1):
        body[f"referenceImage_{i}"] = ref
    for i, ch in enumerate(character_ids or [], start=1):
        body[f"character_{i}"] = ch
    for i, v in enumerate(voice_names or [], start=1):
        body[f"referenceAudio_{i}"] = v

    r = await _call("POST", "/videos", json=_clean(body), timeout=300)
    if not r.get("ok"):
        return r
    job_id = r.get("jobid") or r.get("jobId")
    if not wait:
        return {
            "ok": True,
            "jobId": job_id,
            "status": r.get("status", "created"),
            "next": "Call wait_for_job(jobId) or get_job(jobId). Typical time: 60-180 s.",
        }
    return await wait_for_job(job_id)


@mcp.tool()
async def get_job(job_id: str) -> dict[str, Any]:
    """Check an image or video job once. status is created | started | completed | failed."""
    return _summarize_job(await _call("GET", f"/jobs/{quote(job_id, safe='')}"))


@mcp.tool()
async def wait_for_job(job_id: str, timeout_seconds: int = 300, poll_seconds: int = 8) -> dict[str, Any]:
    """Poll a job until it completes or fails (default up to 5 min) and return the
    media URLs. If it times out, call again with the same jobId."""
    deadline = asyncio.get_event_loop().time() + max(10, min(timeout_seconds, 540))
    last: dict[str, Any] = {}
    while True:
        last = await _call("GET", f"/jobs/{quote(job_id, safe='')}")
        if not last.get("ok") or last.get("status") in ("completed", "failed"):
            return _summarize_job(last)
        if asyncio.get_event_loop().time() >= deadline:
            s = _summarize_job(last)
            s["timedOut"] = True
            s["next"] = "Still running. Call wait_for_job again with the same jobId."
            return s
        await asyncio.sleep(max(3, poll_seconds))


@mcp.tool()
async def list_recent_jobs() -> dict[str, Any]:
    """List recent jobs on this subscription (useful to recover a lost jobId)."""
    r = await _call("GET", "/jobs", params={"options": "history"})
    if not r.get("ok"):
        return r
    out = []
    for kind in ("videos", "images"):
        sect = r.get(kind) or {}
        for bucket in ("executing", "history"):
            for jid, j in (sect.get(bucket) or {}).items():
                j = j if isinstance(j, dict) else {}
                out.append(_clean({"jobId": jid, "type": kind[:-1], "bucket": bucket, "status": j.get("status"),
                                   "created": j.get("created"), "prompt": (j.get("request") or {}).get("prompt")}))
    out.sort(key=lambda x: x.get("created") or "", reverse=True)
    return {"ok": True, "jobs": out[:50]}


@mcp.tool()
async def upscale_video(media_generation_id: str, resolution: Literal["720p", "1080p", "4K"] = "1080p", wait: bool = True) -> dict[str, Any]:
    """Upscale a generated video by mediaGenerationId to 720p/1080p (any paid plan) or 4K (Ultra)."""
    body = {"mediaGenerationId": media_generation_id, "resolution": resolution, "async": True}
    r = await _call("POST", "/videos/upscale", json=body, timeout=300)
    if not r.get("ok") or not wait:
        return r
    return await wait_for_job(r.get("jobid") or r.get("jobId"))


@mcp.tool()
async def extend_video(
    media_generation_id: str,
    prompt: str,
    model: Literal["veo-3.1-fast", "veo-3.1-quality", "veo-3.1-lite", "veo-3.1-lite-low-priority"] = "veo-3.1-fast",
    wait: bool = True,
) -> dict[str, Any]:
    """Extend a Veo video by ~8 s with a continuation prompt (Veo only; for
    omni-flash clips use generate_video with reference_video_id instead)."""
    body = _clean(
        {
            "mediaGenerationId": media_generation_id,
            "prompt": prompt,
            "model": model,
            "async": True,
        }
    )
    r = await _call("POST", "/videos/extend", json=body, timeout=300)
    if not r.get("ok") or not wait:
        return r
    return await wait_for_job(r.get("jobid") or r.get("jobId"))


@mcp.tool()
async def video_to_gif(media_generation_id: str) -> dict[str, Any]:
    """Convert a generated video to an animated GIF, stored on this server and returned as a downloadUrl."""
    r = await _call("POST", "/videos/gif", json={"mediaGenerationId": media_generation_id}, timeout=300)
    if not r.get("ok"):
        return r
    if not r.get("encodedGif"):
        return {"ok": False, "error": "No GIF returned"}
    return {"ok": True, **_save_bytes(base64.b64decode(r["encodedGif"]), f"clip-{uuid.uuid4().hex[:8]}", "gif")}


# --------------------------------------------------------------------------- #
# assets, voices, characters
# --------------------------------------------------------------------------- #
@mcp.tool()
async def upload_asset(url: str, email: Optional[str] = None) -> dict[str, Any]:
    """Upload an image (png/jpg/webp, ≤20 MB) or MP4 (≤100 MB) from a public URL into the
    Google Flow account so it can be used as a start frame, reference, character image,
    or V2V source. Returns the mediaGenerationId to pass to generate_image / generate_video."""
    acct, err = await _resolve_email(email)
    if err:
        return err
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        src = await client.get(url)
    if src.status_code >= 400:
        return {"ok": False, "error": f"Could not download source URL ({src.status_code})"}
    ctype = (src.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype == "image/jpg":
        ctype = "image/jpeg"
    if ctype not in ("image/png", "image/jpeg", "image/webp", "video/mp4"):
        low = url.lower().split("?")[0]
        ctype = ("image/png" if low.endswith(".png") else "image/webp" if low.endswith(".webp")
                 else "video/mp4" if low.endswith(".mp4") else "image/jpeg")
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{BASE}/assets/{quote(acct, safe='')}", headers={**HEADERS, "Content-Type": ctype}, content=src.content)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "error": body}
    mg = body.get("mediaGenerationId")
    if isinstance(mg, dict):
        mg = mg.get("mediaGenerationId")
    return _clean({"ok": True, "mediaGenerationId": mg, "type": ctype, "width": body.get("width"),
                   "height": body.get("height"), "durationSeconds": body.get("durationSeconds")})


@mcp.tool()
async def get_asset_url(media_generation_id: str) -> dict[str, Any]:
    """Get a fresh signed download URL (~6 h) for any image or video
    mediaGenerationId. Use when an earlier URL has expired or was missing."""
    return await _call("GET", f"/assets/{quote(media_generation_id, safe='')}")


@mcp.tool()
async def list_voices(email: Optional[str] = None, source: Literal["all", "system", "user"] = "all") -> dict[str, Any]:
    """List narration voices: the 30 system presets and any custom voices on the account."""
    acct, err = await _resolve_email(email)
    if err:
        return err
    return await _call("GET", "/voices", params=_clean({"email": acct, "source": None if source == "all" else source}))


@mcp.tool()
async def list_characters(email: Optional[str] = None) -> dict[str, Any]:
    """List reusable characters saved on the account (refs usable as character_ids)."""
    acct, err = await _resolve_email(email)
    if err:
        return err
    r = await _call("GET", "/characters", params={"email": acct})
    if not r.get("ok"):
        return r
    chars = [_clean({"characterId": c.get("character"), "name": c.get("displayName"),
                     "voice": (c.get("voice") or {}).get("voice") or (c.get("voice") or {}).get("displayName"),
                     "images": len(c.get("imageReferences") or []), "created": c.get("createTime")})
             for c in (r.get("characters") or [])]
    return {"ok": True, "characters": chars}


@mcp.tool()
async def create_character(
    name: str,
    image_ids: list[str],
    voice: Optional[str] = None,
    personality_notes: Optional[str] = None,
) -> dict[str, Any]:
    """Save a reusable character from 1-2 image mediaGenerationIds (front view +
    optional second angle). Optionally attach a voice (system preset like 'Kore'
    or a custom voice ref) and personality notes. Returns the character id to pass
    to generate_video / generate_image as character_ids."""
    body: dict[str, Any] = {
        "displayName": name[:200],
        "imageReference_1": image_ids[0],
        "voice": voice,
        "personalityNotes": (personality_notes or "")[:2000] or None,
    }
    if len(image_ids) > 1:
        body["imageReference_2"] = image_ids[1]
    r = await _call("POST", "/characters", json=_clean(body), timeout=300)
    if not r.get("ok"):
        return r
    return {
        "ok": True,
        "characterId": r.get("character"),
        "name": r.get("displayName"),
        "voice": r.get("voice"),
        "images": r.get("imageReferences"),
    }


# --------------------------------------------------------------------------- #
# one-sentence workflows: design a character, produce a whole episode
# --------------------------------------------------------------------------- #
def _save_mp4(b64: str, stem: str) -> dict[str, Any]:
    return _save_bytes(base64.b64decode(b64), stem, "mp4")


@mcp.tool()
async def design_character(
    name: str,
    description: str,
    voice: Optional[str] = "Kore",
    personality_notes: Optional[str] = None,
    style: str = "photorealistic, cinematic lighting",
    email: Optional[str] = None,
) -> dict[str, Any]:
    """ONE CALL: turn a text description into a saved, reusable character.

    Generates a clean front-view portrait and a matching three-quarter view with
    nano-banana-pro (same outfit, plain background), then saves them as a character
    with the chosen voice. Returns the characterId plus preview image URLs so the
    user can approve the look. Use this when the user says things like "create a
    character called Ada, a 30-year-old teacher with short hair and a red hoodie".
    Costs 2 image generations (free on any Google AI plan) and no video credits.
    """
    base = (
        f"{description}. {style}. Plain neutral grey studio background, soft even "
        f"lighting, full head and shoulders visible, same outfit in every shot, "
        f"no text, no watermark."
    )
    front = await generate_image(
        prompt=f"Front-facing character reference portrait, looking at camera. {base}",
        model="nano-banana-pro", aspect_ratio="3:4", count=1, email=email,
    )
    if not front.get("ok") or not front.get("images"):
        return {"ok": False, "step": "front_image", "error": front}
    front_id = front["images"][0]["mediaGenerationId"]

    side = await generate_image(
        prompt=(
            "The exact same person as @reference_1, identical face, hair and outfit, "
            f"now in a three-quarter view turned slightly to the left, natural relaxed smile. {base}"
        ),
        model="nano-banana-pro", aspect_ratio="3:4", count=1,
        reference_ids=[front_id], email=email,
    )
    image_ids = [front_id]
    if side.get("ok") and side.get("images"):
        image_ids.append(side["images"][0]["mediaGenerationId"])

    ch = await create_character(name=name, image_ids=image_ids, voice=voice, personality_notes=personality_notes)
    if not ch.get("ok"):
        return {"ok": False, "step": "create_character", "error": ch, "imageIds": image_ids}

    return {
        "ok": True,
        "characterId": ch["characterId"],
        "name": name,
        "voice": voice,
        "previewImages": [front["images"][0].get("url")] + ([side["images"][0].get("url")] if len(image_ids) > 1 else []),
        "imageIds": image_ids,
        "next": "Use this characterId in produce_episode or generate_video(character_ids=[...]). "
                "If the user dislikes the look, call design_character again with a refined description.",
    }


@mcp.tool()
async def concatenate_videos(
    media_ids: list[str],
    trim_start: Optional[list[float]] = None,
    trim_end: Optional[list[float]] = None,
    filename: Optional[str] = None,
) -> dict[str, Any]:
    """Join 2-10 generated video clips (same account, same aspect ratio) into one
    MP4, in order. Optional per-clip trims in seconds. The result is stored on this
    server and returned as a downloadUrl (valid as long as the server keeps the file)."""
    media = []
    for i, mid in enumerate(media_ids[:10]):
        item: dict[str, Any] = {"mediaGenerationId": mid}
        if trim_start and i < len(trim_start) and trim_start[i]:
            item["trimStart"] = trim_start[i]
        if trim_end and i < len(trim_end) and trim_end[i]:
            item["trimEnd"] = trim_end[i]
        media.append(item)
    r = await _call("POST", "/videos/concatenate", json={"media": media}, timeout=600)
    if not r.get("ok"):
        return r
    if not r.get("encodedVideo"):
        return {"ok": False, "error": "No video returned", "raw": {k: v for k, v in r.items() if k != "encodedVideo"}}
    stem = (filename or f"episode-{int(time.time())}").replace("/", "_").removesuffix(".mp4")
    saved = _save_mp4(r["encodedVideo"], stem)
    return {"ok": True, "jobId": r.get("jobId"), "clips": r.get("inputsCount"), **saved}


# ---- episode runner (background) ------------------------------------------ #
EPISODES: dict[str, dict[str, Any]] = {}


def _ep_path(ep_id: str) -> Path:
    return DATA_DIR / "episodes" / f"{ep_id}.json"


def _ep_save(ep: dict[str, Any]) -> None:
    EPISODES[ep["episodeId"]] = ep
    _ep_path(ep["episodeId"]).write_text(json.dumps(ep, indent=2))


def _ep_load(ep_id: str) -> Optional[dict[str, Any]]:
    if ep_id in EPISODES:
        return EPISODES[ep_id]
    p = _ep_path(ep_id)
    if p.exists():
        EPISODES[ep_id] = json.loads(p.read_text())
        return EPISODES[ep_id]
    return None


async def _run_episode(ep_id: str) -> None:
    ep = _ep_load(ep_id)
    assert ep
    sem = asyncio.Semaphore(ep["concurrency"])

    async def one(i: int, scene: dict[str, Any]) -> None:
        async with sem:
            scene["status"] = "generating"
            _ep_save(ep)
            prompt = scene["prompt"]
            if "@character_1" not in prompt.lower():
                prompt = f"@character_1 {prompt}"
            for attempt in range(1, ep["retries"] + 2):
                r = await generate_video(
                    prompt=prompt,
                    model=ep["model"],
                    aspect_ratio=ep["aspectRatio"],
                    duration=scene.get("duration") or ep["duration"],
                    resolution=ep["resolution"],
                    character_ids=[ep["characterId"]],
                    voice_names=[ep["voice"]] if ep.get("voice") else None,
                    seed=ep.get("seed"),
                    email=ep.get("email"),
                    wait=True,
                )
                vids = [v for v in (r.get("videos") or []) if v.get("mediaGenerationId")]
                if r.get("ok") and vids and not r.get("timedOut"):
                    scene.update(status="done", mediaGenerationId=vids[0]["mediaGenerationId"],
                                 videoUrl=vids[0].get("videoUrl"), attempts=attempt)
                    _ep_save(ep)
                    return
                scene["lastError"] = r.get("error") or r.get("failureReasons") or r.get("status")
                wait_s = r.get("retryAfterSeconds") or 20
                await asyncio.sleep(min(wait_s, 120))
            scene["status"] = "failed"
            _ep_save(ep)

    await asyncio.gather(*(one(i, s) for i, s in enumerate(ep["scenes"])))

    done = [s for s in ep["scenes"] if s["status"] == "done"]
    if len(done) != len(ep["scenes"]):
        ep["status"] = "partial"
        ep["message"] = f"{len(done)}/{len(ep['scenes'])} scenes succeeded. Call retry_episode to redo failed scenes."
        _ep_save(ep)
        return

    if ep["concatenate"] and len(done) >= 2:
        ep["status"] = "joining"
        _ep_save(ep)
        joined = await concatenate_videos(
            [s["mediaGenerationId"] for s in ep["scenes"]],
            trim_start=[ep["trimStart"] if i > 0 else 0 for i in range(len(ep["scenes"]))],
            trim_end=[ep["trimEnd"] if i < len(ep["scenes"]) - 1 else 0 for i in range(len(ep["scenes"]))],
            filename=f"{ep['slug']}-{ep_id[:8]}",
        )
        if joined.get("ok"):
            ep["final"] = joined
            ep["status"] = "completed"
        else:
            ep["status"] = "partial"
            ep["message"] = "All scenes generated but joining failed; individual clips are available."
            ep["joinError"] = joined
    else:
        ep["status"] = "completed"
    _ep_save(ep)


@mcp.tool()
async def produce_episode(
    title: str,
    character_id: str,
    scenes: list[str],
    voice: Optional[str] = None,
    model: Literal["omni-flash", "veo-3.1-fast", "veo-3.1-lite", "veo-3.1-lite-low-priority"] = "omni-flash",
    duration: Literal[4, 6, 8, 10] = 8,
    aspect_ratio: Literal["landscape", "portrait"] = "landscape",
    resolution: Literal["720p", "360p"] = "720p",
    concatenate: bool = True,
    seed: Optional[int] = None,
    concurrency: int = 2,
    email: Optional[str] = None,
) -> dict[str, Any]:
    """ONE CALL: render a whole YouTube episode with a consistent character.

    Give a saved characterId and a list of scene prompts (one per clip; describe the
    ACTION, SETTING, CAMERA and what the character SAYS in quotes — not their looks,
    the character reference handles that). The server generates every clip in the
    background with the same character + voice, retries failures, then joins them
    into one MP4 and returns a download link.

    Returns immediately with an episodeId. Poll get_episode(episodeId) every ~60 s
    (a 10-scene episode takes roughly 8-15 minutes). Credits: omni-flash 8 s ≈ 12
    credits per scene; use veo-3.1-lite-low-priority for free clips on Ultra $199.
    aspect_ratio 'portrait' for Shorts, 'landscape' for normal YouTube.
    """
    if not scenes:
        return {"ok": False, "error": "scenes is empty"}
    if len(scenes) > 40:
        return {"ok": False, "error": "Max 40 scenes per episode; split it up."}
    ep_id = uuid.uuid4().hex
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "episode"
    ep = {
        "episodeId": ep_id, "title": title, "slug": slug, "status": "running",
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "characterId": character_id, "voice": voice, "model": model, "duration": duration,
        "aspectRatio": aspect_ratio, "resolution": resolution if model == "omni-flash" else "720p",
        "concatenate": concatenate and len(scenes) >= 2, "seed": seed, "email": email,
        "concurrency": max(1, min(concurrency, 4)), "retries": 2,
        "trimStart": 0.0, "trimEnd": 0.0,
        "scenes": [{"index": i, "prompt": p, "status": "queued"} for i, p in enumerate(scenes)],
    }
    _ep_save(ep)
    asyncio.create_task(_run_episode(ep_id))
    est = len(scenes) * 150 / ep["concurrency"]
    return {
        "ok": True, "episodeId": ep_id, "status": "running", "scenes": len(scenes),
        "estimatedMinutes": round(est / 60, 1),
        "next": "Call get_episode(episodeId) in about a minute, then every 60 s until status is completed.",
    }


@mcp.tool()
async def get_episode(episode_id: str) -> dict[str, Any]:
    """Status of a produce_episode run: per-scene progress, clip URLs, and the final
    joined MP4 downloadUrl when completed. Status: running | joining | completed | partial."""
    ep = _ep_load(episode_id)
    if not ep:
        return {"ok": False, "error": "Unknown episodeId"}
    counts: dict[str, int] = {}
    for s in ep["scenes"]:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {
        "ok": True, "episodeId": episode_id, "title": ep["title"], "status": ep["status"],
        "progress": counts, "message": ep.get("message"),
        "final": ep.get("final"),
        "scenes": [
            _clean({"index": s["index"], "status": s["status"], "videoUrl": s.get("videoUrl"),
                    "mediaGenerationId": s.get("mediaGenerationId"), "lastError": s.get("lastError")})
            for s in ep["scenes"]
        ],
    }


@mcp.tool()
async def retry_episode(episode_id: str, scene_indexes: Optional[list[int]] = None) -> dict[str, Any]:
    """Re-run failed scenes of an episode (or specific scene indexes, e.g. to redo a
    clip the user didn't like) and re-join the final MP4."""
    ep = _ep_load(episode_id)
    if not ep:
        return {"ok": False, "error": "Unknown episodeId"}
    if ep["status"] in ("running", "joining"):
        return {"ok": False, "error": "Episode is still running"}
    targets = set(scene_indexes or [s["index"] for s in ep["scenes"] if s["status"] != "done"])
    for s in ep["scenes"]:
        if s["index"] in targets:
            s.update(status="queued", mediaGenerationId=None, videoUrl=None, lastError=None)
    want_join = bool(ep.get("concatenate"))
    ep["status"] = "running"
    ep.pop("final", None); ep.pop("message", None)
    _ep_save(ep)

    async def _rerun() -> None:
        # Pull done scenes aside, run the generator on the queued ones, merge back.
        keep = {s["index"]: s for s in ep["scenes"] if s["status"] == "done"}
        ep["scenes"] = [s for s in ep["scenes"] if s["status"] == "queued"]
        ep["concatenate"] = False  # join after merging, below
        _ep_save(ep)
        await _run_episode(episode_id)
        ep2 = _ep_load(episode_id)
        merged = sorted(list(keep.values()) + ep2["scenes"], key=lambda s: s["index"])
        ep2["scenes"] = merged
        ep2["concatenate"] = want_join
        if all(s["status"] == "done" for s in merged):
            if want_join and len(merged) >= 2:
                ep2["status"] = "joining"; _ep_save(ep2)
                joined = await concatenate_videos([s["mediaGenerationId"] for s in merged],
                                                  filename=f"{ep2['slug']}-{episode_id[:8]}")
                ep2["final"] = joined if joined.get("ok") else None
                ep2["status"] = "completed" if joined.get("ok") else "partial"
            else:
                ep2["status"] = "completed"
        else:
            ep2["status"] = "partial"
            ep2["message"] = "Some scenes still failed. Call retry_episode again."
        _ep_save(ep2)

    asyncio.create_task(_rerun())
    return {"ok": True, "episodeId": episode_id, "retrying": sorted(targets)}


@mcp.tool()
async def list_episodes() -> dict[str, Any]:
    """List episodes produced on this server with status and final download links."""
    out = []
    for p in sorted((DATA_DIR / "episodes").glob("*.json"), reverse=True)[:50]:
        ep = json.loads(p.read_text())
        out.append({"episodeId": ep["episodeId"], "title": ep["title"], "status": ep["status"],
                    "created": ep["created"], "downloadUrl": (ep.get("final") or {}).get("downloadUrl")})
    return {"ok": True, "episodes": out}


# --------------------------------------------------------------------------- #
# plain HTTP routes: serve finished MP4s, health check
# --------------------------------------------------------------------------- #
@mcp.custom_route(f"{prefix}/files/{{name}}", methods=["GET"])
async def serve_file(request: Request) -> Response:
    name = Path(request.path_params["name"]).name
    p = DATA_DIR / "files" / name
    types = {".mp4": "video/mp4", ".gif": "image/gif", ".jpg": "image/jpeg"}
    ext = p.suffix.lower()
    if not p.exists() or ext not in types:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, media_type=types[ext], filename=name)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    print(f"Google Flow MCP listening on 0.0.0.0:{PORT}{http_path}", flush=True)
    if not PUBLIC_URL:
        print("WARNING: PUBLIC_URL not set — download links will be relative. Set it to your https host.", flush=True)
    mcp.run(transport="streamable-http")
