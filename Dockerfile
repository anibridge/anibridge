FROM python:3.14-alpine AS python-builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/venv \
    UV_PYTHON_PREFERENCE=only-system \
    VIRTUAL_ENV=/venv

WORKDIR /app

RUN apk add --no-cache git

COPY ./src ./LICENSE ./README.md /app/

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock,ro \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml,ro \
    uv sync --frozen --no-dev && \
    find /venv -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null

FROM --platform=$BUILDPLATFORM node:25-alpine AS node-builder

WORKDIR /app

ENV CI=1 \
    PNPM_HOME=/pnpm \
    PNPM_STORE_DIR=/pnpm/store
ENV PATH="$PNPM_HOME:$PATH"

RUN npm install -g pnpm

RUN --mount=type=bind,source=frontend/pnpm-lock.yaml,target=/app/pnpm-lock.yaml,ro \
    --mount=type=bind,source=frontend/package.json,target=/app/package.json,ro \
    --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile

COPY ./frontend /app

RUN pnpm build

FROM python:3.14-alpine

RUN apk add --no-cache shadow su-exec tzdata

LABEL maintainer="Elias Benbourenane <eliasbenbourenane@gmail.com>" \
    org.opencontainers.image.title="AniBridge" \
    org.opencontainers.image.description="The smart way to keep your anime lists perfectly synchronized." \
    org.opencontainers.image.authors="Elias Benbourenane <eliasbenbourenane@gmail.com>" \
    org.opencontainers.image.url="https://anibridge.eliasbenb.dev" \
    org.opencontainers.image.documentation="https://anibridge.eliasbenb.dev" \
    org.opencontainers.image.source="https://github.com/anibridge/anibridge" \
    org.opencontainers.image.licenses="MIT"

ENV PATH=/venv/bin:$PATH \
    PYTHONMALLOC=malloc \
    PYTHONUNBUFFERED=1 \
    PYTHON_JIT=1 \
    PUID=1000 \
    PGID=1000 \
    UMASK=022 \
    AB_DATA_PATH=/config

WORKDIR /app

COPY . /app
COPY ./scripts/docker_init.sh /init

COPY --from=python-builder /venv /venv
COPY --from=node-builder /app/build /app/frontend/build

RUN mkdir -p /config

VOLUME ["/config"]

EXPOSE 4848

ENTRYPOINT ["/init"]
CMD ["python", "/app/main.py"]
