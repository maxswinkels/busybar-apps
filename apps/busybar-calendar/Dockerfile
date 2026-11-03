FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --system app
USER app
ENTRYPOINT ["/app/.venv/bin/busybar-calendar", "--config", "/config/config.toml"]
CMD ["run"]
