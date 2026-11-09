FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY world ./world
ENV BANK_DB=/data/bank.db
EXPOSE 8081
CMD [".venv/bin/uvicorn", "world.mock_bank.app:from_env", "--factory", "--host", "0.0.0.0", "--port", "8081"]
