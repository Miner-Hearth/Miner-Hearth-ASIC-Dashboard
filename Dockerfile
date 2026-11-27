# Miner Hearth: headless web dashboard for Linux servers, Docker, Umbrel and 5tratumOS.
# The program needs only the Python standard library; no packages are installed.
#   docker build -t miner-hearth .
#   docker run -d --name miner-hearth -p 8080:8080 -v miner-hearth-data:/data miner-hearth
# Multi-arch (amd64 + arm64): .github/workflows/docker.yml builds and pushes ghcr.io/<owner>/miner-hearth.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

ARG VERSION=1.17.1
LABEL org.opencontainers.image.title="Miner Hearth" \
      org.opencontainers.image.source="https://github.com/Miner-Hearth/Miner-Hearth-ASIC-Dashboard" \
      org.opencontainers.image.description="Dashboard, monitor and autotuner for home ASIC miners (headless web edition)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MINER_HEARTH_HOME=/data \
    MINER_HEARTH_HEADLESS=1 \
    MINER_HEARTH_HOST=0.0.0.0 \
    MINER_HEARTH_PORT=8080 \
    MINER_HEARTH_RESTART_BY_EXIT=1

WORKDIR /app
COPY --chown=0:0 VERSION config.json README.md LICENSE ./
COPY --chown=0:0 *.py ./
COPY --chown=0:0 nk_adapters ./nk_adapters
COPY --chown=0:0 assets ./assets
RUN rm -f test_*.py mock_server.py \
 && python -m compileall -q /app \
 && mkdir -p /data && chown 1000:1000 /data

USER 1000:1000
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/status' % os.environ.get('MINER_HEARTH_PORT','8080'), timeout=8)" || exit 1

ENTRYPOINT ["python", "/app/miner_dashboard.py", "--headless"]
