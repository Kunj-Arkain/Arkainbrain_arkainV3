# ARKAINBRAIN — Railway Deployment Guide

## One-Service Setup (Simplest)

This runs the web server + job worker in a single Railway service. Jobs execute as subprocesses within the container.

---

### Step 1: Push Code to GitHub

Make sure your repo includes these files:
```
Dockerfile       ← builds the container
start.sh         ← starts web + worker
web_app.py       ← Flask app
worker.py        ← job processor
config/          ← database, settings
admin/           ← admin panel
tools/           ← cost tracker, etc.
requirements.txt
```

### Step 2: Create Railway Project

1. Go to [railway.app](https://railway.app) → **New Project**
2. **Deploy from GitHub repo** → select your repo
3. Railway auto-detects the `Dockerfile` and builds

### Step 3: Set Environment Variables

In your Railway service → **Variables** tab, add:

```env
# ── REQUIRED ──
OPENAI_API_KEY=sk-...
SERPER_API_KEY=...

# ── GOOGLE AUTH (required for login) ──
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...

# ── ADMIN SETUP (use once, then delete) ──
ADMIN_SETUP_SECRET=pick-any-random-string

# ── OPTIONAL ──
ELEVENLABS_API_KEY=...           # AI sound effects
QDRANT_URL=...                   # Vector DB
QDRANT_API_KEY=...

# ── RAILWAY AUTO-SETS THESE ──
# PORT (auto-set by Railway)
# RAILWAY_ENVIRONMENT (auto-set)
```

**Important:** Railway auto-generates a `PORT` variable. Don't set it manually.

### Step 4: Add PostgreSQL (Recommended)

1. In your Railway project → **+ New** → **Database** → **PostgreSQL**
2. Click the PostgreSQL service → **Variables** → copy `DATABASE_URL`
3. Go to your web service → **Variables** → add:
```env
DATABASE_URL=postgresql://...  (paste from step 2)
```

If you skip this, the app uses SQLite (works but doesn't persist across deploys).

### Step 5: Add Redis (Optional but Recommended)

Redis gives you a proper job queue instead of subprocess fallback.

1. In your Railway project → **+ New** → **Database** → **Redis**
2. Click the Redis service → **Variables** → copy `REDIS_URL`
3. Go to your web service → **Variables** → add:
```env
REDIS_URL=redis://...  (paste from step 2)
```

The `start.sh` script auto-detects Redis and launches an RQ worker alongside the web server.

### Step 6: Deploy

Push to GitHub. Railway auto-builds and deploys.

Check the **Logs** tab to see:
```
╔══════════════════════════════════════════════╗
║  ARKAINBRAIN — Starting Production Server    ║
╚══════════════════════════════════════════════╝
PORT=8080
→ Running DB init + migrations...
  DB ready.
→ Redis detected — starting RQ worker in background...
  RQ worker started (PID=42)
→ Starting gunicorn on port 8080...
```

### Step 7: Generate Your Public URL

1. Go to your service → **Settings** → **Networking**
2. Click **Generate Domain** → you get `something.up.railway.app`

### Step 8: Update Google OAuth Redirect

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **Credentials**
2. Edit your OAuth client
3. Add to **Authorized redirect URIs**:
```
https://your-app.up.railway.app/auth/callback
```

### Step 9: Activate Admin

1. Visit your app and log in with Google
2. Then visit:
```
https://your-app.up.railway.app/setup-admin/pick-any-random-string
```
(Use whatever you set as `ADMIN_SETUP_SECRET`)

3. You'll see "✅ Admin Activated"
4. Go to Railway → **Variables** → **delete** `ADMIN_SETUP_SECRET` (security)

The 🔒 Admin link now appears in your sidebar.

---

## Troubleshooting

### Jobs stuck on "Waiting for worker to start..."

**Diagnose:** Visit `https://your-app.up.railway.app/health/workers`

This shows:
- Queue mode (redis vs subprocess)
- Stuck/failed jobs
- Worker log files with error messages

**Common fixes:**

1. **No Redis + subprocess failing silently:**
   - Add Redis (Step 5 above) — this is the most reliable fix
   - Or check worker logs at `/health/workers` for the actual error

2. **Missing API keys:**
   - The worker subprocess inherits env vars. Check that `OPENAI_API_KEY` is set in Railway Variables

3. **Out of memory:**
   - Railway free tier has limited RAM. Upgrade or reduce `--workers` in start.sh

4. **Database locked (SQLite only):**
   - Add PostgreSQL (Step 4). SQLite doesn't handle concurrent access well

### Jobs fail immediately

Check `/health/workers` for error logs. Common causes:
- Missing `OPENAI_API_KEY` or `SERPER_API_KEY`
- API rate limits hit
- Insufficient memory for LLM calls

### Can't log in

- Make sure `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set
- Make sure the redirect URI in Google Console matches your Railway domain exactly
- Must include `https://` and `/auth/callback`

### Admin panel shows 403

- You need to activate admin first (Step 9)
- If you already did, log out and log back in to refresh your session

---

## Environment Variable Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | Yes | GPT + DALL-E API calls |
| `SERPER_API_KEY` | Yes | Web search for research |
| `GOOGLE_CLIENT_ID` | Yes | Google OAuth login |
| `GOOGLE_CLIENT_SECRET` | Yes | Google OAuth login |
| `DATABASE_URL` | Recommended | PostgreSQL connection string |
| `REDIS_URL` | Recommended | Redis for reliable job queue |
| `ADMIN_SETUP_SECRET` | One-time | Secret for `/setup-admin/` route |
| `ELEVENLABS_API_KEY` | Optional | AI sound effects |
| `QDRANT_URL` | Optional | Vector database |
| `QDRANT_API_KEY` | Optional | Vector database auth |
| `MAX_CONCURRENT_JOBS` | Optional | Max parallel jobs (default: 6) |
| `LOG_DIR` | Optional | Log directory (default: ./logs) |
| `FLASK_DEBUG` | Optional | Enable debug mode (default: false) |

---

## Architecture on Railway

### With Redis (recommended):
```
┌─────────────────────────────┐
│  Railway Service (single)   │
│  start.sh launches:         │
│  ├── gunicorn (web server)  │
│  └── rq worker (background) │
├─────────────────────────────┤
│  Railway PostgreSQL          │
├─────────────────────────────┤
│  Railway Redis               │
└─────────────────────────────┘
```

### Without Redis:
```
┌─────────────────────────────┐
│  Railway Service (single)   │
│  start.sh launches:         │
│  └── gunicorn (web server)  │
│      └── subprocess workers │
├─────────────────────────────┤
│  Railway PostgreSQL          │
│  (or SQLite in container)   │
└─────────────────────────────┘
```

---

## Updating

Push to GitHub → Railway auto-redeploys. Zero-downtime if you have the health check enabled (it is by default at `/health`).
