# Detection Lambda image (Story 2.1, AD-12): AWS Lambda Python 3.14 base, runtime deps from uv.lock.
# Build from the repo root: docker build --platform linux/amd64 -f detect/Dockerfile .
# Runtime deps only: no `dev` or `infra` groups (pyproject.toml makes them default groups).
FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM public.ecr.aws/lambda/python:3.14

# Resolve from the lock into a throwaway environment, then copy the packages into the task root,
# which is first on the Lambda sys.path (ahead of the runtime's own boto3).
ENV UV_PROJECT_ENVIRONMENT=/tmp/venv \
    UV_PYTHON=/var/lang/bin/python3.14 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

# uv and the lock files are bind-mounted for this step only, so none of them ends up in a layer.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=pyproject.toml,target=/build/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/build/uv.lock \
    cd /build && uv sync --locked --no-default-groups --no-install-project \
    && cp -r /tmp/venv/lib/python3.14/site-packages/. "${LAMBDA_TASK_ROOT}/" \
    && rm -rf /tmp/venv

COPY facility/ ${LAMBDA_TASK_ROOT}/facility/
COPY detect/ ${LAMBDA_TASK_ROOT}/detect/

CMD ["detect.handler.handler"]
