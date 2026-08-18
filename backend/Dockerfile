# syntax=docker/dockerfile:1
# One image, three roles (migrate / api / worker). Same bytes run in every service, so
# a passing API image and a failing worker image can never diverge.
FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, in their own layer: application edits do not invalidate the
# dependency install, which is the slow half of the build.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY ledgerloop ./ledgerloop
COPY scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# Non-root: nothing in this container needs to write outside its own venv.
RUN useradd --create-home --uid 10001 ledgerloop && chown -R ledgerloop:ledgerloop /app
USER ledgerloop

EXPOSE 8000 9100

CMD ["uvicorn", "ledgerloop.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
