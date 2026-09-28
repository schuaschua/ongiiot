# Simulator image (Story 1.4, AD-10): python:3.14-slim with runtime deps from uv.lock.
# Build from the repo root: docker build --platform linux/amd64 -f sim/Dockerfile .
# Runtime deps only: no `dev` or `infra` groups (pyproject.toml makes them default groups).
FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.14-slim

COPY --from=uv /uv /usr/local/bin/uv

# Install into the image's own Python (/usr/local), so `pip list` shows exactly what runs.
# --inexact keeps the base image's pip.
ENV UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-default-groups --no-install-project --inexact

COPY facility/ facility/
COPY sim/ sim/

RUN useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 sim
USER sim

CMD ["python", "-m", "sim.main"]
