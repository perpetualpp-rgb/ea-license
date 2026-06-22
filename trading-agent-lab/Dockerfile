# Trading Agent Lab / Flows Agent — deployable runner.
#
# Safe by default: the image ships config.json with broker.mode="paper", so a
# bare `docker run` only paper-trades. Real data (TradingView) and real LLM
# agents (Hermes) activate when you provide the keys via the environment.
# Live trading additionally needs broker.mode=live + ALLOW_LIVE=1 + exchange
# keys + dry_run:false (see README "Going live").
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist the Obsidian vault (notes + per-symbol state) outside the container.
VOLUME ["/app/vault"]

# Self-looping session: one cycle every INTERVAL seconds. Override the command
# to use cron / --once, a different --symbol, or --live instead.
ENV INTERVAL=180
ENTRYPOINT ["sh", "-c", "exec python3 flow.py --interval \"$INTERVAL\""]
