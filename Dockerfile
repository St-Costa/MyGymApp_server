FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY reparse.py ./

RUN pip install --no-cache-dir .

# Data root is expected to be bind-mounted at runtime, e.g.:
#   docker run -v ~/mygymapp-server-data:/data -e DATA_ROOT=/data ...
ENV DATA_ROOT=/data
ENV PORT=8420

# Bind to 127.0.0.1 inside the container is meaningless across the container
# boundary — Tailscale Serve talks to the published port on the host, and the
# host-side port publish (-p 127.0.0.1:8420:8420) is what actually enforces
# "only reachable via localhost". See docs/sync-ingestion/SPEC.md.
EXPOSE 8420

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8420"]
