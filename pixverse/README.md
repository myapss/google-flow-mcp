# PixVerse MCP server (Seedance 2.5 / 2.0, Kling O3 …)

Same idea as the Google Flow server one folder up, but driving a **PixVerse** account via
useapi.net. Characters are stored on this server as reference-image sets; episodes are
joined with FFmpeg (PixVerse has no join endpoint).

## Env vars
| Var | Value |
|---|---|
| `USEAPI_TOKEN` | useapi.net token |
| `MCP_ACCESS_KEY` | long random secret → URL becomes `/<key>/mcp` |
| `PUBLIC_URL` | https address of this deployment |
| `DATA_DIR` | `/app/data` (mount a volume here) |

## Connect PixVerse to useapi.net
1. Create a PixVerse account with **email + password** (not Google/Apple/Discord login) and buy a plan (Ultra recommended for third-party models).
2. `POST https://api.useapi.net/v2/pixverse/accounts/<email>` with `{ "email", "password", "maxJobs": 3 }` — or use the form at https://useapi.net/docs/start-here/setup-pixverse

## Tools
`design_character`, `create_character_from_images`, `list_characters`, `produce_episode`,
`get_episode`, `retry_episode`, `generate_video`, `get_video`, `generate_image`, `upload_asset`,
`join_videos`, `estimate_cost`, `get_credits`, `list_accounts`, `list_episodes`.

## Railway
Deploy this repo as a second service with **Root Directory = `pixverse`**.
