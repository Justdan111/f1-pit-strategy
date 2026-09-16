# syntax=docker/dockerfile:1

# Two stages: the builder resolves dependencies, the runtime carries only the
# finished virtualenv and source. uv and the build cache never ship.

FROM python:3.11-slim-bookworm AS builder

# Pinned, not :latest — two builds of one commit must use the same uv.
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies before source, so a code-only change keeps this layer cached.
COPY pyproject.toml uv.lock ./

# --frozen installs exactly what uv.lock pins and fails if it disagrees with
# pyproject.toml, so the container runs the versions that were tested.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.11-slim-bookworm AS runtime

RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src

# A writable data directory. /app is created by WORKDIR and owned by root, so
# the non-root user cannot write there -- which silently disabled the decision
# log in the container until this was caught. Keeping the source tree
# non-writable is worth keeping: the app cannot modify its own code.
#
# Mount a volume here to make the log survive a redeploy.
RUN mkdir -p /data && chown app:app /data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    F1_DECISION_LOG_PATH=/data/decisions.db

USER app

EXPOSE 8000

# `exec` makes uvicorn PID 1 so it receives SIGTERM directly; otherwise the
# shell swallows it and open streams are severed by a SIGKILL instead of
# closed. `sh -c` is needed so ${PORT} is expanded at run time.
CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
