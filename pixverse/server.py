"""
PixVerse MCP server — wraps the useapi.net PixVerse API v2 so Claude.ai can make
consistent-character YouTube episodes with Seedance 2.5 / 2.0, Kling O3, etc.

Env vars:
  USEAPI_TOKEN     required  — useapi.net token ("user:1234-...")
  MCP_ACCESS_KEY   optional  — secret path segment: /<key>/mcp
  PUBLIC_URL       optional  — https host of this deployment (for download links)
  DATA_DIR         optional  — where characters, episodes and MP4s are kept (default ./data)
  PORT             optional  — default 8000
  DEFAULT_EMAIL    optional  — pin every request to one PixVerse account
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

BASE = "https://api.useapi.net/v2/pixverse"
TOKEN = os.environ.get("USEAPI_TOKEN", "").strip()
ACCESS_KEY = os.environ.get("MCP_ACCESS_KEY", "").strip()
DEFAULT_EMAIL = os.environ.get("DEFAULT_EMAIL", "").strip() or None
PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
for sub in ("episodes", "files", "characters", "tmp"):
    (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)

if not TOKEN:
    raise SystemExit("USEAPI_TOKEN is not set")

prefix = f"/{ACCESS_KEY}" if ACCESS_KEY else ""
http_path = f"{prefix}/mcp"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

mcp = FastMCP(
    "PixVerse (useapi.net)",
    instructions=(
        "Generate videos with Seedance 2.5 / 2.0, Kling O3, PixVerse v6 and more through the "
        "user's PixVerse account via useapi.net, with consistent characters. "
        "Workflow: design_character (once) -> produce_episode (per video) -> get_episode until "
        "completed -> hand the user the downloadUrl. Clips are 5-15 s each; a 15-minute episode "
        "is ~90 scenes and takes 1-2 hours. Media URLs from PixVerse are stable."
    ),
    host="0.0.0.0", port=PORT, streamable_http_path=http_path,
    stateless_http=True, json_response=True,
)

VIDEO_MODELS = Literal[
    "seedance-2.5", "seedance-2.0", "seedance-2.0-fast", "seedance-2.0-mini",
    "kling-o3", "minimax-h3", "wan-3.0", "pixverse-c1", "v5.6",
]
# how many reference images each fusion model accepts
REF_LIMIT = {"seedance-2.5": 30, "seedance-2.0": 9, "seedance-2.0-fast": 9, "seedance-2.0-mini": 9,
             "kling-o3": 7, "minimax-h3": 9, "wan-3.0": 10, "pixverse-c1": 7, "v5.6": 7}
# which models accept the `audio` toggle (seedance-2.5 does NOT)
AUDIO_TOGGLE = {"seedance-2.0", "seedance-2.0-fast", "seedance-2.0-mini", "kling-o3", "wan-3.0", "pixverse-c1", "v5.6"}
# credits per second at 720p (list price, before plan discounts) — for estimates only
CREDITS_PER_SEC_720 = {"seedance-2.5": 75, "seedance-2.0": 45, "seedance-2.0-fast": 30, "seedance-2.0-mini": 18,
                       "kling-o3": 25, "minimax-h3": 30, "wan-3.0": 30, "pixverse-c1": 30, "v5.6": 30}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _file_url(name: str) -> str:
    return f"{PUBLIC_URL}{prefix}/files/{name}" if PUBLIC_URL else f"{prefix}/files/{name}"


async def _call(method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                timeout: float = 120.0) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(method, f"{BASE}{path}", headers=HEADERS, json=json, params=params)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "error": body}
    return {"ok": True, "status": r.status_code, **(body if isinstance(body, dict) else {"data": body})}


async def _resolve_email(email: Optional[str]) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    acct = email or DEFAULT_EMAIL
    if acct:
        return acct, None
    r = await _call("GET", "/accounts")
    if not r.get("ok"):
        return None, r
    emails = [k for k in r if "@" in k]
    if len(emails) == 1:
        return emails[0], None
    return None, {"ok": False, "error": "Specify `email` — more than one PixVerse account is connected.", "accounts": emails}


async def _upload_bytes(data: bytes, ctype: str, email: Optional[str]) -> dict[str, Any]:
    """POST /files with raw bytes. Returns {'ok', 'path', 'url'} (image path from result[0].path)."""
    acct, err = await _resolve_email(email)
    if err:
        return err
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{BASE}/files/", params={"email": acct},
                              headers={**HEADERS, "Content-Type": ctype}, content=data)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "error": body}
    if isinstance(body, dict) and isinstance(body.get("result"), list) and body["result"]:
        item = body["result"][0]
        return {"ok": True, "path": item.get("path"), "url": item.get("url"), "id": item.get("id")}
    return {"ok": True, "path": body.get("path"), "url": body.get("url")}


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


def _guess_ctype(url: str, header: str) -> str:
    c = (header or "").split(";")[0].strip().lower()
    if c in ("image/png", "image/jpeg", "image/webp", "image/gif", "video/mp4", "audio/mpeg", "audio/wav"):
        return "image/jpeg" if c == "image/jpg" else c
    u = url.lower().split("?")[0]
    for ext, t in ((".png", "image/png"), (".webp", "image/webp"), (".gif", "image/gif"), (".mp4", "video/mp4"),
                   (".mp3", "audio/mpeg"), (".wav", "audio/wav")):
        if u.endswith(ext):
            return t
    return "image/jpeg"


async def _poll_video(video_id: str, timeout_s: int = 900, every: float = 10.0) -> dict[str, Any]:
    """Poll GET /videos/{id}. 404 = still processing. Returns a trimmed summary."""
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        r = await _call("GET", f"/videos/{video_id}")
        if r.get("ok"):
            last = r
            name = r.get("video_status_name")
            if name == "COMPLETED" and r.get("url"):
                return {"ok": True, "videoId": video_id, "status": "completed", "url": r["url"],
                        "duration": r.get("video_duration") or r.get("duration"),
                        "width": r.get("output_width"), "height": r.get("output_height")}
            if name == "MODERATED":
                return {"ok": False, "videoId": video_id, "status": "moderated", "error": "Rejected by PixVerse moderation"}
            if r.get("error") and r.get("video_status_final"):
                return {"ok": False, "videoId": video_id, "status": "failed", "error": r.get("error")}
        elif r.get("status") not in (404,):
            return {"ok": False, "videoId": video_id, "status": "error", "error": r.get("error")}
        await asyncio.sleep(every)
    return {"ok": False, "videoId": video_id, "status": "timeout", "timedOut": True,
            "lastStatus": last.get("video_status_name"), "queue": last.get("queue_data")}


async def _poll_image(image_id: str, timeout_s: int = 300) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = await _call("GET", f"/images/{image_id}")
        if r.get("ok"):
            if r.get("image_status_name") == "COMPLETED" and r.get("image_url"):
                return {"ok": True, "imageId": image_id, "url": r["image_url"], "path": r.get("image_path")}
            if r.get("image_status_name") == "MODERATED":
                return {"ok": False, "error": "Image moderated"}
        elif r.get("status") != 404:
            return r
        await asyncio.sleep(5)
    return {"ok": False, "error": "Image generation timed out"}


async def _submit_with_retry(path: str, body: dict[str, Any], tries: int = 12) -> dict[str, Any]:
    """POST a generation; on 429 (queue full / concurrency) wait and retry."""
    for i in range(tries):
        r = await _call("POST", path, json=body, timeout=120)
        if r.get("ok") or r.get("status") != 429:
            return r
        await asyncio.sleep(20 + 5 * i)
    return r


def _ffmpeg_concat(paths: list[Path], out: Path) -> dict[str, Any]:
    """Join MP4s. Try stream copy first; fall back to re-encode if streams differ."""
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in paths))
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst)]
    r = subprocess.run(base + ["-c", "copy", "-movflags", "+faststart", str(out)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
        r = subprocess.run(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                                   "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)],
                           capture_output=True, text=True)
    lst.unlink(missing_ok=True)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-2000:]}
    return {"ok": True}


# --------------------------------------------------------------------------- #
# characters (stored on this server)
# --------------------------------------------------------------------------- #
def _char_path(cid: str) -> Path:
    return DATA_DIR / "characters" / f"{cid}.json"


def _char_load(cid: str) -> Optional[dict[str, Any]]:
    p = _char_path(cid)
    return json.loads(p.read_text()) if p.exists() else None


def _char_save(c: dict[str, Any]) -> None:
    _char_path(c["characterId"]).write_text(json.dumps(c, indent=2))


# --------------------------------------------------------------------------- #
# account tools
# --------------------------------------------------------------------------- #
@mcp.tool()
async def list_accounts() -> dict[str, Any]:
    """List PixVerse accounts connected to this useapi.net subscription."""
    r = await _call("GET", "/accounts")
    if not r.get("ok"):
        return r
    return {"ok": True, "accounts": [{"email": k, "maxJobs": (v or {}).get("maxJobs")} for k, v in r.items() if "@" in k]}


@mcp.tool()
async def get_credits(email: Optional[str] = None) -> dict[str, Any]:
    """Show the PixVerse account's remaining credits and plan (via GET accounts/email and
    the credits endpoint when available)."""
    acct, err = await _resolve_email(email)
    if err:
        return err
    r = await _call("GET", f"/accounts/{acct}")
    out: dict[str, Any] = {"ok": r.get("ok"), "email": acct, "maxJobs": r.get("maxJobs")}
    for key in ("credits", "credit", "balance", "plan", "subscription", "features"):
        if key in r:
            out[key] = r[key]
    feat = await _call("GET", "/features", params={"email": acct})
    if feat.get("ok"):
        out["features"] = {k: v for k, v in feat.items() if k not in ("ok", "status")}
    return out


# --------------------------------------------------------------------------- #
# assets & images
# --------------------------------------------------------------------------- #
@mcp.tool()
async def upload_asset(url: str, email: Optional[str] = None) -> dict[str, Any]:
    """Upload an image / MP4 / MP3 from a public URL into PixVerse. Returns the `path` to use
    as a reference (character image, start frame, reference video or voice sample)."""
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        src = await client.get(url)
    if src.status_code >= 400:
        return {"ok": False, "error": f"Could not download source ({src.status_code})"}
    return await _upload_bytes(src.content, _guess_ctype(url, src.headers.get("content-type", "")), email)


@mcp.tool()
async def generate_image(
    prompt: str,
    model: Literal["nano-banana-pro", "nano-banana-2", "seedream-4.5", "seedream-5.0-pro", "qwen-image", "gpt-image-2.0"] = "nano-banana-pro",
    aspect_ratio: str = "3:4",
    quality: str = "1080p",
    count: int = 1,
    reference_paths: Optional[list[str]] = None,
    seed: Optional[int] = None,
    email: Optional[str] = None,
) -> dict[str, Any]:
    """Generate 1-4 images (10-25 credits each). Use for character portraits, thumbnails,
    start frames. Returns image URLs plus `path` values usable as references."""
    body: dict[str, Any] = {"prompt": prompt, "model": model, "aspect_ratio": aspect_ratio, "quality": quality,
                            "create_count": max(1, min(4, count)), "seed": seed, "email": email or DEFAULT_EMAIL}
    for i, p in enumerate(reference_paths or [], start=1):
        body[f"image_path_{i}"] = p
    r = await _submit_with_retry("/images/create", _clean(body))
    if not r.get("ok"):
        return r
    ids = r.get("success_ids") or ([r["image_id"]] if r.get("image_id") else [])
    results = []
    for iid in ids:
        res = await _poll_image(iid)
        results.append({"imageId": iid, **{k: v for k, v in res.items() if k != "ok"}, "ok": res.get("ok")})
    return {"ok": any(x.get("ok") for x in results), "images": results}


# --------------------------------------------------------------------------- #
# characters
# --------------------------------------------------------------------------- #
@mcp.tool()
async def design_character(
    name: str,
    description: str,
    style: str = "photorealistic, cinematic lighting",
    views: int = 3,
    voice_sample_url: Optional[str] = None,
    personality_notes: Optional[str] = None,
    email: Optional[str] = None,
) -> dict[str, Any]:
    """ONE CALL: text description -> saved reusable character.

    Generates a front portrait, then matching three-quarter and profile views with
    nano-banana-pro (same face/outfit, plain background), uploads them to PixVerse as
    reference images, and saves the character on this server. Every later scene made with
    produce_episode attaches these references so the character stays the same.
    voice_sample_url (optional MP3/WAV, 2-15 s of the voice you want) is attached as a
    reference audio on models that support it (Seedance). Costs ~3 image credits × 25.
    """
    base = (f"{description}. {style}. Plain neutral grey studio background, soft even lighting, "
            f"head and shoulders, same outfit in every shot, no text, no watermark.")
    front = await generate_image(prompt=f"Front-facing character reference portrait, looking at camera. {base}",
                                 model="nano-banana-pro", aspect_ratio="3:4", count=1, email=email)
    if not front.get("ok"):
        return {"ok": False, "step": "front", "error": front}
    f0 = front["images"][0]
    # re-upload generated images so we hold stable reference `path`s
    async def to_ref(url: str) -> Optional[str]:
        up = await _upload_bytes(await _download(url), "image/webp" if url.endswith(".webp") else "image/jpeg", email)
        return up.get("path") if up.get("ok") else None
    paths: list[str] = []
    previews: list[str] = []
    p0 = await to_ref(f0["url"])
    if not p0:
        return {"ok": False, "step": "upload_front", "error": "Could not upload reference"}
    paths.append(p0); previews.append(f0["url"])
    extra_prompts = [
        "The exact same person as the reference, identical face, hair and outfit, three-quarter view turned slightly left, relaxed natural smile.",
        "The exact same person as the reference, identical face, hair and outfit, side profile view, neutral expression.",
    ][: max(0, min(views, 3) - 1)]
    for ep in extra_prompts:
        img = await generate_image(prompt=f"{ep} {base}", model="nano-banana-pro", aspect_ratio="3:4", count=1,
                                   reference_paths=[p0], email=email)
        if img.get("ok"):
            u = img["images"][0]["url"]
            p = await to_ref(u)
            if p:
                paths.append(p); previews.append(u)
    voice_path = None
    if voice_sample_url:
        v = await upload_asset(voice_sample_url, email)
        voice_path = v.get("path") if v.get("ok") else None
    cid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] + "-" + uuid.uuid4().hex[:6]
    char = {"characterId": cid, "name": name, "description": description, "style": style,
            "referencePaths": paths, "previewUrls": previews, "voicePath": voice_path,
            "personalityNotes": personality_notes, "email": email or DEFAULT_EMAIL,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _char_save(char)
    return {"ok": True, "characterId": cid, "name": name, "referenceImages": len(paths), "previewImages": previews,
            "voiceAttached": bool(voice_path),
            "next": "Show the previews to the user. If approved, use this characterId in produce_episode."}


@mcp.tool()
async def create_character_from_images(name: str, image_urls: list[str], voice_sample_url: Optional[str] = None,
                                       email: Optional[str] = None) -> dict[str, Any]:
    """Save a character from the user's OWN photos/renders (1-9 public image URLs) instead of
    generating them. Returns a characterId for produce_episode."""
    paths = []
    for u in image_urls[:9]:
        up = await upload_asset(u, email)
        if up.get("ok") and up.get("path"):
            paths.append(up["path"])
    if not paths:
        return {"ok": False, "error": "No images could be uploaded"}
    voice_path = None
    if voice_sample_url:
        v = await upload_asset(voice_sample_url, email)
        voice_path = v.get("path") if v.get("ok") else None
    cid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] + "-" + uuid.uuid4().hex[:6]
    char = {"characterId": cid, "name": name, "referencePaths": paths, "previewUrls": image_urls[:len(paths)],
            "voicePath": voice_path, "email": email or DEFAULT_EMAIL,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _char_save(char)
    return {"ok": True, "characterId": cid, "referenceImages": len(paths), "voiceAttached": bool(voice_path)}


@mcp.tool()
async def list_characters() -> dict[str, Any]:
    """List characters saved on this server."""
    out = []
    for p in sorted((DATA_DIR / "characters").glob("*.json")):
        c = json.loads(p.read_text())
        out.append({"characterId": c["characterId"], "name": c["name"], "referenceImages": len(c["referencePaths"]),
                    "voiceAttached": bool(c.get("voicePath")), "created": c.get("created"), "preview": (c.get("previewUrls") or [None])[0]})
    return {"ok": True, "characters": out}


# --------------------------------------------------------------------------- #
# single video
# --------------------------------------------------------------------------- #
@mcp.tool()
async def generate_video(
    prompt: str,
    character_id: Optional[str] = None,
    model: VIDEO_MODELS = "seedance-2.5",
    duration: int = 10,
    quality: Literal["480p", "720p", "1080p"] = "720p",
    aspect_ratio: Literal["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"] = "16:9",
    audio: Optional[bool] = None,
    extra_reference_paths: Optional[list[str]] = None,
    reference_video_paths: Optional[list[str]] = None,
    email: Optional[str] = None,
    wait: bool = True,
) -> dict[str, Any]:
    """Generate one clip. With character_id the character's reference images (and voice
    sample, if any) are attached automatically — mention them in the prompt as @image1 etc.
    seedance-2.5: 4-30 s, best consistency, no generated speech (use reference audio or add
    voice-over later). seedance-2.0 family / kling-o3: audio=true gives spoken dialogue from
    quoted lines in the prompt. Cost ≈ credits/sec × duration (seedance-2.5 720p = 75/s)."""
    refs: list[str] = []
    voice = None
    acct = email or DEFAULT_EMAIL
    if character_id:
        c = _char_load(character_id)
        if not c:
            return {"ok": False, "error": f"Unknown characterId {character_id}"}
        refs = list(c["referencePaths"]); voice = c.get("voicePath"); acct = acct or c.get("email")
    refs += list(extra_reference_paths or [])
    limit = REF_LIMIT.get(model, 7)
    refs = refs[:limit]
    body: dict[str, Any] = {"prompt": prompt, "model": model, "duration": duration, "quality": quality,
                            "aspect_ratio": aspect_ratio, "email": acct}
    if refs:
        for i, p in enumerate(refs, start=1):
            body[f"frame_{i}_path"] = p
        for i, p in enumerate(reference_video_paths or [], start=1):
            body[f"video_{i}_path"] = p
        if voice and model.startswith("seedance") or (voice and model == "wan-3.0"):
            body["audio_1_path"] = voice
        if "@image1" not in prompt.lower():
            body["prompt"] = f"@image1 is the main character; keep their exact face, hair and outfit. {prompt}"
        if audio is not None and model in AUDIO_TOGGLE:
            body["audio"] = audio
        r = await _submit_with_retry("/videos/create-fusion", _clean(body))
    else:
        if audio is not None and model in AUDIO_TOGGLE:
            body["audio"] = audio
        r = await _submit_with_retry("/videos/create", _clean(body))
    if not r.get("ok"):
        return r
    vid = r.get("video_id")
    if not wait:
        return {"ok": True, "videoId": vid, "status": "submitted", "next": "Call get_video(videoId)."}
    return await _poll_video(vid)


@mcp.tool()
async def get_video(video_id: str) -> dict[str, Any]:
    """Check one clip. While PixVerse is still generating it returns status 'processing'."""
    r = await _call("GET", f"/videos/{video_id}")
    if not r.get("ok"):
        if r.get("status") == 404:
            return {"ok": True, "videoId": video_id, "status": "processing"}
        return r
    return {"ok": True, "videoId": video_id, "status": (r.get("video_status_name") or "").lower(),
            "url": r.get("url"), "duration": r.get("video_duration"), "queue": r.get("queue_data"), "error": r.get("error")}


@mcp.tool()
async def estimate_cost(model: VIDEO_MODELS = "seedance-2.5", quality: Literal["480p", "720p", "1080p"] = "720p",
                        minutes: float = 15, retake_percent: int = 25) -> dict[str, Any]:
    """Rough credit estimate for N minutes of finished video at list price (plan discounts
    apply on Ultra: -20% seedance-2.5, -40% other third-party)."""
    per_sec = CREDITS_PER_SEC_720.get(model, 30)
    factor = {"480p": 0.47, "720p": 1.0, "1080p": 2.1}[quality]
    secs = minutes * 60 * (1 + retake_percent / 100)
    credits = int(secs * per_sec * factor)
    return {"ok": True, "model": model, "quality": quality, "secondsGenerated": int(secs), "creditsListPrice": credits,
            "ultraPlansNeeded": round(credits / 25000, 2), "note": "Ultra discount not applied; see useapi.net PixVerse pricing."}


# --------------------------------------------------------------------------- #
# episodes
# --------------------------------------------------------------------------- #
def _ep_path(ep_id: str) -> Path:
    return DATA_DIR / "episodes" / f"{ep_id}.json"


def _ep_save(ep: dict[str, Any]) -> None:
    _ep_path(ep["episodeId"]).write_text(json.dumps(ep, indent=2))


def _ep_load(ep_id: str) -> Optional[dict[str, Any]]:
    p = _ep_path(ep_id)
    return json.loads(p.read_text()) if p.exists() else None


async def _join_episode(ep: dict[str, Any]) -> dict[str, Any]:
    tmp = DATA_DIR / "tmp" / ep["episodeId"]
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for s in sorted(ep["scenes"], key=lambda x: x["index"]):
        if s["status"] != "done":
            continue
        p = tmp / f"{s['index']:03d}.mp4"
        if not p.exists():
            p.write_bytes(await _download(s["videoUrl"]))
        paths.append(p)
    if not paths:
        return {"ok": False, "error": "No finished clips"}
    name = f"{ep['slug']}-{ep['episodeId'][:8]}.mp4"
    out = DATA_DIR / "files" / name
    res = await asyncio.to_thread(_ffmpeg_concat, paths, out)
    shutil.rmtree(tmp, ignore_errors=True)
    if not res.get("ok"):
        return res
    return {"ok": True, "file": name, "bytes": out.stat().st_size, "clips": len(paths), "downloadUrl": _file_url(name)}


async def _run_episode(ep_id: str) -> None:
    ep = _ep_load(ep_id)
    assert ep
    sem = asyncio.Semaphore(ep["concurrency"])

    async def one(scene: dict[str, Any]) -> None:
        async with sem:
            scene["status"] = "generating"; _ep_save(ep)
            for attempt in range(1, ep["retries"] + 2):
                r = await generate_video(prompt=scene["prompt"], character_id=ep["characterId"], model=ep["model"],
                                         duration=scene.get("duration") or ep["duration"], quality=ep["quality"],
                                         aspect_ratio=ep["aspectRatio"], audio=ep.get("audio"), email=ep.get("email"), wait=True)
                if r.get("ok") and r.get("url"):
                    scene.update(status="done", videoId=r["videoId"], videoUrl=r["url"], attempts=attempt); _ep_save(ep); return
                scene["lastError"] = r.get("error") or r.get("status")
                if r.get("status") == "moderated" and attempt <= ep["retries"]:
                    # soften the prompt a little on moderation retries
                    scene["prompt"] = scene["prompt"] + " Family-friendly, safe for all audiences."
                await asyncio.sleep(15)
            scene["status"] = "failed"; _ep_save(ep)

    await asyncio.gather(*(one(s) for s in ep["scenes"] if s["status"] == "queued"))
    done = [s for s in ep["scenes"] if s["status"] == "done"]
    if len(done) != len(ep["scenes"]):
        ep["status"] = "partial"
        ep["message"] = f"{len(done)}/{len(ep['scenes'])} scenes succeeded. Call retry_episode to redo failed scenes."
        _ep_save(ep); return
    if ep["concatenate"] and len(done) >= 1:
        ep["status"] = "joining"; _ep_save(ep)
        joined = await _join_episode(ep)
        if joined.get("ok"):
            ep["final"] = joined; ep["status"] = "completed"
        else:
            ep["status"] = "partial"; ep["message"] = "All scenes generated but joining failed."; ep["joinError"] = joined
    else:
        ep["status"] = "completed"
    _ep_save(ep)


@mcp.tool()
async def produce_episode(
    title: str,
    character_id: str,
    scenes: list[str],
    model: VIDEO_MODELS = "seedance-2.5",
    duration: int = 10,
    quality: Literal["480p", "720p", "1080p"] = "720p",
    aspect_ratio: Literal["16:9", "9:16"] = "16:9",
    audio: Optional[bool] = None,
    concatenate: bool = True,
    concurrency: int = 2,
    email: Optional[str] = None,
) -> dict[str, Any]:
    """ONE CALL: render a whole episode with a consistent character and join it into ONE MP4.

    scenes: one prompt per clip describing ACTION, SETTING, CAMERA and (for audio models)
    dialogue in quotes — not the character's looks; the saved references handle that.
    duration: seconds per clip (seedance-2.5: 4-30; 2.0 family: 4-15; kling-o3: 3-15).
    A 15-minute episode ≈ 90 scenes × 10 s and takes 1-2 h. Returns immediately with an
    episodeId; poll get_episode every ~2 min. Cost ≈ credits/sec × total seconds
    (seedance-2.5 720p: 75/s list → ~7,000 credits per minute before Ultra discount).
    Use estimate_cost first for long episodes.
    """
    if not scenes:
        return {"ok": False, "error": "scenes is empty"}
    if len(scenes) > 200:
        return {"ok": False, "error": "Max 200 scenes per episode"}
    if not _char_load(character_id):
        return {"ok": False, "error": f"Unknown characterId {character_id}. Call list_characters."}
    ep_id = uuid.uuid4().hex
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "episode"
    ep = {"episodeId": ep_id, "title": title, "slug": slug, "status": "running",
          "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "characterId": character_id, "model": model, "duration": duration, "quality": quality,
          "aspectRatio": aspect_ratio, "audio": audio, "concatenate": concatenate, "email": email,
          "concurrency": max(1, min(concurrency, 6)), "retries": 2,
          "scenes": [{"index": i, "prompt": p, "status": "queued"} for i, p in enumerate(scenes)]}
    _ep_save(ep)
    asyncio.create_task(_run_episode(ep_id))
    est = len(scenes) * (duration * 12 + 60) / ep["concurrency"]
    return {"ok": True, "episodeId": ep_id, "scenes": len(scenes), "estimatedMinutes": round(est / 60),
            "estimatedCreditsListPrice": int(len(scenes) * duration * CREDITS_PER_SEC_720.get(model, 30) * {"480p": 0.47, "720p": 1, "1080p": 2.1}[quality]),
            "next": "Call get_episode(episodeId) in a couple of minutes, then every ~2 min until status is completed."}


@mcp.tool()
async def get_episode(episode_id: str) -> dict[str, Any]:
    """Progress of an episode: per-scene status, clip URLs, and the final joined MP4
    downloadUrl when completed. Status: running | joining | completed | partial."""
    ep = _ep_load(episode_id)
    if not ep:
        return {"ok": False, "error": "Unknown episodeId"}
    counts: dict[str, int] = {}
    for s in ep["scenes"]:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {"ok": True, "episodeId": episode_id, "title": ep["title"], "status": ep["status"], "progress": counts,
            "message": ep.get("message"), "final": ep.get("final"),
            "scenes": [_clean({"index": s["index"], "status": s["status"], "videoUrl": s.get("videoUrl"), "lastError": s.get("lastError")}) for s in ep["scenes"]]}


@mcp.tool()
async def retry_episode(episode_id: str, scene_indexes: Optional[list[int]] = None,
                        new_prompts: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Re-run failed scenes (or the given indexes, optionally with new prompts keyed by
    index as a string) and re-join the final MP4."""
    ep = _ep_load(episode_id)
    if not ep:
        return {"ok": False, "error": "Unknown episodeId"}
    if ep["status"] in ("running", "joining"):
        return {"ok": False, "error": "Episode is still running"}
    targets = set(scene_indexes or [s["index"] for s in ep["scenes"] if s["status"] != "done"])
    for s in ep["scenes"]:
        if s["index"] in targets:
            s.update(status="queued", videoId=None, videoUrl=None, lastError=None)
            if new_prompts and str(s["index"]) in new_prompts:
                s["prompt"] = new_prompts[str(s["index"])]
    ep["status"] = "running"; ep.pop("final", None); ep.pop("message", None)
    _ep_save(ep)
    asyncio.create_task(_run_episode(episode_id))
    return {"ok": True, "episodeId": episode_id, "retrying": sorted(targets)}


@mcp.tool()
async def join_videos(video_urls: list[str], filename: Optional[str] = None) -> dict[str, Any]:
    """Join any list of MP4 URLs (PixVerse clip URLs or others) into one MP4 with FFmpeg."""
    tmp = DATA_DIR / "tmp" / uuid.uuid4().hex
    tmp.mkdir(parents=True)
    paths = []
    for i, u in enumerate(video_urls):
        p = tmp / f"{i:03d}.mp4"; p.write_bytes(await _download(u)); paths.append(p)
    name = (filename or f"joined-{int(time.time())}").removesuffix(".mp4") + ".mp4"
    out = DATA_DIR / "files" / name
    res = await asyncio.to_thread(_ffmpeg_concat, paths, out)
    shutil.rmtree(tmp, ignore_errors=True)
    if not res.get("ok"):
        return res
    return {"ok": True, "file": name, "clips": len(paths), "bytes": out.stat().st_size, "downloadUrl": _file_url(name)}


@mcp.tool()
async def list_episodes() -> dict[str, Any]:
    """List episodes produced on this server with status and download links."""
    out = []
    for p in sorted((DATA_DIR / "episodes").glob("*.json"), reverse=True)[:50]:
        ep = json.loads(p.read_text())
        out.append({"episodeId": ep["episodeId"], "title": ep["title"], "status": ep["status"], "created": ep["created"],
                    "downloadUrl": (ep.get("final") or {}).get("downloadUrl")})
    return {"ok": True, "episodes": out}


# --------------------------------------------------------------------------- #
# HTTP routes
# --------------------------------------------------------------------------- #
@mcp.custom_route(f"{prefix}/files/{{name}}", methods=["GET"])
async def serve_file(request: Request) -> Response:
    name = Path(request.path_params["name"]).name
    p = DATA_DIR / "files" / name
    types = {".mp4": "video/mp4", ".gif": "image/gif", ".jpg": "image/jpeg"}
    if not p.exists() or p.suffix.lower() not in types:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, media_type=types[p.suffix.lower()], filename=name)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse({"ok": True, "ffmpeg": shutil.which("ffmpeg") is not None})


if __name__ == "__main__":
    print(f"PixVerse MCP listening on 0.0.0.0:{PORT}{http_path}", flush=True)
    if not shutil.which("ffmpeg"):
        print("WARNING: ffmpeg not found — joining will fail", flush=True)
    mcp.run(transport="streamable-http")
