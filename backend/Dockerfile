# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Backend image.
#
# Why a Dockerfile rather than Render's Python buildpack (DAY5.md): the
# buildpack detects pyproject.toml and guesses a run command, and what it
# guesses is not something this repo controls or can reproduce locally. With
# an image, the bytes that run on Render are the bytes verified on this
# machine. That is the whole reason it is here.
#
# Two stages. The builder resolves and installs dependencies; the runtime
# carries only the finished virtualenv and the source. uv itself, the build
# toolchain and the layer cache never reach the shipped image.
# ---------------------------------------------------------------------------

# --- stage 1: build the virtualenv ----------------------------------------
FROM python:3.11-slim-bookworm AS builder

# uv is copied from its own published image at a PINNED version. `:latest`
# would mean two builds of the same commit could use different uv versions,
# which is the exact reproducibility problem this file exists to solve.
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /uvx /bin/

# Copy the venv into the image instead of symlinking to a cached interpreter,
# so stage 2 can take it wholesale.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, source second. Editing application code then leaves the
# dependency layer cached, so a code-only rebuild does not re-resolve or
# re-download anything.
COPY pyproject.toml uv.lock ./

# --frozen: install EXACTLY what uv.lock pins, and fail if the lockfile and
# pyproject.toml disagree. Never re-resolve. This is the line that makes the
# container run the same versions that were tested all week rather than
# whatever happens to be newest at build time.
#
# --no-dev: pytest and friends are for the test suite, not for serving.
# --no-install-project: the project itself comes in the next layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src

# Now install the project itself against the already-resolved dependencies.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# --- stage 2: runtime ------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

# Run as a non-root user. If the process is ever compromised it should not
# also own the filesystem.
RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

# Just the virtualenv and the source. No uv, no lockfile, no build cache.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

USER app

# Documentation only; the actual port comes from $PORT at run time.
EXPOSE 8000

# NOTE ON CONFIGURATION: nothing application-specific is baked in here. Every
# setting (F1_OPENF1_BASE_URL, F1_ALLOWED_ORIGINS, poll intervals, pit-lane
# cost) is read from the environment at startup by config.py, so the same
# image runs locally, in CI and on Render with only `-e` flags differing.
# See README "Configuration".
#
# JSON form, wrapping an explicit `sh -c` so ${PORT} is still expanded — Render
# assigns the port at run time and it cannot be hardcoded. `exec` matters: it
# replaces the shell with uvicorn so uvicorn becomes PID 1 and receives SIGTERM
# directly. Without it the shell would hold PID 1, swallow the signal, and every
# deploy would end in a 10-second SIGKILL instead of a graceful shutdown —
# meaning open WebSocket streams would be severed rather than closed.
CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
