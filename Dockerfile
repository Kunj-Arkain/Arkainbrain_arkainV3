FROM python:3.11-slim

WORKDIR /app

# System deps (including PostgreSQL client libs for psycopg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir gunicorn && \
    pip freeze > /app/requirements.lock

# Copy project
COPY . .

# Make start script executable
RUN chmod +x start.sh

# Create output directories + persistent data mount point
RUN mkdir -p output/recon data/regulations/us_states logs /data/output /data/logs

# Pre-create CrewAI config to prevent tracing prompt
RUN mkdir -p /root/.crewai /tmp/crewai_storage && \
    echo '{"tracing_enabled": false, "tracing_disabled": true}' > /root/.crewai/config.json && \
    echo '{"tracing_enabled": false, "tracing_disabled": true}' > /tmp/crewai_storage/config.json

# ═══════════════════════════════════════════════
#  Environment Variables
#  Override at runtime via Railway Variables tab,
#  docker run -e, or docker-compose environment:
# ═══════════════════════════════════════════════

# ── Server ──
ENV PORT=8080
ENV DB_PATH=arkainbrain.db

# ── Admin (REQUIRED for ACP access) ──
ENV ADMIN_EMAIL=""

# ── Auth (at least one pair required for login) ──
ENV GOOGLE_CLIENT_ID=""
ENV GOOGLE_CLIENT_SECRET=""
ENV DISCORD_CLIENT_ID=""
ENV DISCORD_CLIENT_SECRET=""
ENV SESSION_SECRET=""

# ── AI / LLM ──
ENV OPENAI_API_KEY=""
ENV ANTHROPIC_API_KEY=""

# ── Web Search & Market Intel ──
ENV SERPER_API_KEY=""

# ── Audio (optional) ──
ENV ELEVENLABS_API_KEY=""

# ── Vector DB (optional) ──
ENV QDRANT_URL=""
ENV QDRANT_API_KEY=""

# ── Redis queue (optional — falls back to subprocess) ──
ENV REDIS_URL=""

EXPOSE ${PORT:-8080}

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT:-8080}/health')" || exit 1

CMD ["./start.sh"]
