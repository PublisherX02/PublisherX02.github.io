FROM python:3.11-slim

WORKDIR /app

# Copy only the necessary files for the application to run
COPY pyproject.toml ./
COPY src/ ./src/
COPY upstream_runtime/ ./upstream_runtime/

# Install the application, then materialize the separately locked Alpaca
# subprocess environment.  The proxy launches this with `uv run --frozen`,
# so container startup can never re-resolve a newer broker runtime.
RUN pip install --no-cache-dir -e . uv==0.11.20 \
    && uv sync --project /app/upstream_runtime --frozen --no-dev

# The entrypoint is set to run the proxy module.
# We intentionally DO NOT set any environment variables related to ALPACA_PAPER_TRADE
# so the application-level guards handle all security checks without interference.
ENTRYPOINT ["python", "-m", "firewall.proxy"]
