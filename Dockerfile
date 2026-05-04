# CoreMind — all-in-one container (daemon + plugins + bridge)
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install CoreMind and all plugins
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --editable .

COPY src/ src/
COPY plugins/ plugins/
COPY integrations/ integrations/

RUN for p in plugins/*/; do pip install --no-cache-dir --editable "$p" 2>/dev/null || true; done

RUN mkdir -p /root/.coremind/run /root/.coremind/keys /root/.coremind/snapshots /root/.coremind/secrets /root/.coremind/faces /root/.coremind/conversations

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src:/app/plugins:/app/integrations

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
