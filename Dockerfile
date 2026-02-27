FROM alpine:3.23 AS python-builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed \
    VIRTUAL_ENV=/opt/venv

WORKDIR /app

RUN apk add --no-cache git

COPY ./src /app/src
COPY ./README.md /app/README.md

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock,ro \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml,ro \
    uv sync --frozen --no-dev

FROM node:25-alpine AS node-builder

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

FROM alpine:3.23

RUN apk add --no-cache shadow su-exec tzdata

LABEL maintainer="Elias Benbourenane <eliasbenbourenane@gmail.com>" \
    org.opencontainers.image.title="AniBridge" \
    org.opencontainers.image.description="The smart way to keep your anime lists perfectly synchronized." \
    org.opencontainers.image.authors="Elias Benbourenane <eliasbenbourenane@gmail.com>" \
    org.opencontainers.image.url="https://anibridge.eliasbenb.dev" \
    org.opencontainers.image.documentation="https://anibridge.eliasbenb.dev" \
    org.opencontainers.image.source="https://github.com/anibridge/anibridge" \
    org.opencontainers.image.licenses="MIT"

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHON_JIT=1 \
    PUID=1000 \
    PGID=1000 \
    UMASK=022 \
    AB_DATA_PATH=/config

WORKDIR /app

COPY . /app
COPY ./scripts/docker_init.sh /init

RUN rm -rf /app/frontend && \
    mkdir -p /config

COPY --from=python-builder /python /python
COPY --from=python-builder /opt/venv /opt/venv
COPY --from=node-builder /app/build /app/frontend/build

VOLUME ["/config"]

EXPOSE 4848

ENTRYPOINT ["/init"]
CMD ["python", "/app/main.py"]
