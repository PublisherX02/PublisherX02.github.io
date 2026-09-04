FROM python:3.11-slim

WORKDIR /app

# Application code
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY policies/ ./policies/
COPY dashboard/ ./dashboard/
COPY serve_dashboard.py ./

# Frozen public evidence snapshot (real paper-account audit trail, cycle
# artifacts, and order lifecycle) baked into the image only -- the live
# gitignored copies in data/ and audit.jsonl are never touched by this
# build. No credentials, no Alpaca/Featherless keys: this deploy runs the
# real dashboard against static data, with live account panels showing an
# explicit "OFFLINE (public demo snapshot)" state instead of fake numbers.
COPY demo_data/audit.jsonl ./audit.jsonl
COPY demo_data/order_lifecycle.json ./data/order_lifecycle.json
COPY demo_data/last_market_brief.json ./data/last_market_brief.json
COPY demo_data/cycles/ ./data/cycles/

RUN pip install --no-cache-dir -e . textual-serve==1.1.3

ENV PORT=8000
EXPOSE 8000
CMD ["python", "serve_dashboard.py"]
