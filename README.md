# Google Flow MCP server for Claude.ai

A small MCP server that lets Claude.ai generate **Veo 3.1 / Omni 1.1 Flash video** and
**Nano Banana images** through your own Google Flow account, via the
[useapi.net Google Flow API](https://useapi.net/docs/api-google-flow-v1).

You host it (free/cheap tier is fine), add it to Claude.ai as a custom connector,
and then just ask Claude things like *"make an 8-second Veo clip of a surfer at sunset"*.

## What you need first

1. A **useapi.net subscription** ($15/mo) and your API token
   (looks like `user:1234-xxxxxxxx`).
2. Your Google account connected to useapi.net via their
   [Setup Google Flow](https://useapi.net/docs/start-here/setup-google-flow) page
   (use a dedicated Gmail, not your personal one — their docs are strict about this).
3. For **video**, a paid Google AI plan (Plus/Pro/Ultra). Images work on a free account.

## Tools Claude gets

| Tool | What it does |
|---|---|
| `generate_image` | Nano Banana 2 Lite / 2.1 / Pro, 1–4 images, optional references & characters |
| `generate_video` | Veo 3.1 (fast/quality/lite/low-priority) or Omni 1.1 Flash; T2V, I2V, references, voices, V2V edit |
| `wait_for_job` / `get_job` | Poll a video job and return signed MP4 URLs |
| `upload_asset` | Push an image/MP4 from a URL into Flow to use as a frame or reference |
| `get_asset_url` | Re-mint a fresh download link for any media id |
| `upscale_image`, `upscale_video`, `extend_video`, `video_to_gif` | Post-processing |
| **`design_character`** | **One call:** text description → 2 reference portraits → saved character with a fixed voice |
| **`produce_episode`** | **One call:** characterId + list of scene prompts → every clip rendered with that character, auto-retried, joined into one MP4 with a download link |
| `get_episode`, `retry_episode`, `list_episodes` | Track an episode, redo a scene you don't like, find old ones |
| `concatenate_videos` | Join any 2–10 clips into one MP4 |
| `list_voices`, `list_characters`, `create_character` | Reusable voices & characters |
| `list_accounts`, `get_account`, `get_captcha_status`, `list_recent_jobs` | Account health, credits, captcha balance |

## Deploy (pick one)

All options need two environment variables:

| Variable | Value |
|---|---|
| `USEAPI_TOKEN` | your useapi.net token, including the `user:` prefix |
| `MCP_ACCESS_KEY` | a long random string, e.g. output of `openssl rand -hex 24` |

| `PUBLIC_URL` | the https address of your deployment, e.g. `https://xyz.up.railway.app` (used to build download links for finished episodes) |
| `DATA_DIR` | optional, where finished MP4s and episode state are kept (default `./data`). Mount a volume here on Railway/Render/Fly if you want episodes to survive restarts |

`MCP_ACCESS_KEY` becomes part of the URL (`/<key>/mcp`). Anyone who has the URL can spend
your Flow credits, so treat it like a password. Optional: `DEFAULT_EMAIL` to pin all
requests to one connected Google account.

### Railway (easiest)

1. Push this folder to a GitHub repo.
2. Railway → **New Project → Deploy from GitHub repo** → pick it. It detects the Dockerfile.
3. **Settings → Networking → Generate Domain**. Note the URL, e.g. `https://xyz.up.railway.app`.
4. **Variables** tab → add `USEAPI_TOKEN`, `MCP_ACCESS_KEY`, and `PUBLIC_URL` (the URL from step 3).
5. Optional: **Volumes → Add volume** mounted at `/app/data` so finished episodes persist.

### Render

1. Push to GitHub, then Render → **New → Blueprint** → select the repo (`render.yaml` is included).
2. Fill in `USEAPI_TOKEN` when prompted; `MCP_ACCESS_KEY` is auto-generated — copy it from
   the service's Environment tab afterwards.

### Fly.io

```bash
fly launch --no-deploy            # accept defaults, say no to Postgres/Redis
fly secrets set USEAPI_TOKEN="user:1234-..." MCP_ACCESS_KEY="$(openssl rand -hex 24)"
fly deploy
```

### Any VPS / Docker

```bash
docker build -t google-flow-mcp .
docker run -d -p 8000:8000 \
  -e USEAPI_TOKEN="user:1234-..." \
  -e MCP_ACCESS_KEY="$(openssl rand -hex 24)" \
  google-flow-mcp
```
Put it behind HTTPS (Caddy, nginx + certbot, Cloudflare Tunnel) — Claude.ai requires `https://`.

### Quick local test (no hosting)

```bash
pip install -r requirements.txt
USEAPI_TOKEN="user:1234-..." MCP_ACCESS_KEY=test python server.py
# → listening on http://0.0.0.0:8000/test/mcp
```
Expose it with `cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000` to try it
from Claude.ai before committing to a host.

## Connect it to Claude.ai

1. Claude.ai → **Settings → Connectors → Add custom connector**.
2. **Name:** `Google Flow`
3. **Remote MCP server URL:** `https://<your-host>/<MCP_ACCESS_KEY>/mcp`
4. Leave OAuth fields blank → **Add**.
5. Open a new chat, click the **+ / tools** button, make sure *Google Flow* is enabled.

Then try:

> Generate a 16:9 image of a neon-lit ramen shop in the rain with nano-banana-pro.

> Make an 8-second omni-flash clip: a golden retriever narrating a cooking show, voice Puck.
> Wait for it and give me the link.

Claude will call `generate_video`, then `wait_for_job`, and return the signed MP4 URL
(valid ~6 hours — download it if you want to keep it).

## Making a YouTube series with a consistent character

Everything happens in chat once the connector is on. Example session:

> **You:** Create a character called Ada — a 30-year-old Nigerian teacher, short natural hair,
> red hoodie, warm smile. Voice Kore.
>
> *Claude calls `design_character` → shows you two preview portraits and saves Ada.*
>
> **You:** Looks good. Make episode 1, "Why is the sky blue", landscape, 8-second scenes:
> 1. Ada at her desk says "Hey everyone, welcome back!"
> 2. Ada points at a whiteboard diagram of sunlight hitting the atmosphere
> 3. Ada holds a prism, light splits into colours
> 4. Ada waves: "See you next time!"
>
> *Claude calls `produce_episode` → polls `get_episode` → hands you the MP4 download link.*
>
> **You:** Redo scene 3, the prism looked odd.
>
> *Claude calls `retry_episode(scene_indexes=[2])` → new joined MP4.*

Next episode, just say "make episode 2 with Ada" — the character id is saved on your Google
account, so she looks and sounds the same every time. `list_characters` shows everyone you've made.

**Tips for consistency**
- Describe the *scene, action, camera and dialogue* in each scene prompt — not the character's
  appearance. The reference images handle that; re-describing can fight them.
- `omni-flash` is the right model for character work (native voice, 4–10 s, up to 7 references).
- Keep one outfit per character. Make a second character (same person, different outfit) if you
  need a costume change.
- Use `aspect_ratio: portrait` for Shorts, `landscape` for regular videos.

## Notes & gotchas

- **Finished episodes are stored on your server**, not on Google. Download them; if you don't
  mount a volume, a redeploy wipes `./data`. Individual clip URLs from Google expire after ~6 h.

- **Credits:** Veo Quality is 100 credits per 8 s clip; Lite is 5–10; Omni 1.1 Flash 4–15.
  Ask Claude to call `get_account` to see your balance before a batch.
- **Captcha:** the first account gets 300 free solves. After that add a captcha-provider key
  on useapi.net (`get_captcha_status` shows what's left).
- **Signed URLs expire** after ~6 h. `get_asset_url` re-mints one from a `mediaGenerationId`.
- **`backend: labs`** in `list_accounts` means the Google account must be re-added on
  useapi.net (Google retired the old backend in Sept 2026).
- Long video waits: `wait_for_job` blocks up to 5 min per call; if it times out, Claude just
  calls it again with the same `jobId`.
- Rotate `MCP_ACCESS_KEY` any time by changing the env var and updating the URL in Claude.ai.
