FROM python:3.11-slim

WORKDIR /app

# Copy only the necessary files for the application to run
COPY pyproject.toml ./
COPY src/ ./src/

# Install the application and its runtime dependencies
RUN pip install --no-cache-dir -e .

# The entrypoint is set to run the proxy module.
# We intentionally DO NOT set any environment variables related to ALPACA_PAPER_TRADE
# so the application-level guards handle all security checks without interference.
ENTRYPOINT ["python", "-m", "firewall.proxy"]
