# One image, two entrypoints: `stock-agent-api` (agent) and `stock-agent-mcp` (MCP server).
FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels .

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data CHARTS_DIR=/app/charts MPLBACKEND=Agg
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home /app app
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels stock-agent && rm -rf /wheels
WORKDIR /app
RUN mkdir -p /app/data /app/charts && chown -R app:app /app
USER app
EXPOSE 8000 8080
# Overridden per service in docker-compose / k8s probes.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(os.environ.get('HEALTHCHECK_URL','http://127.0.0.1:8080/healthz'), timeout=2)"
CMD ["stock-agent-api"]
