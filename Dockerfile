# syntax=docker/dockerfile:1
# Multi-stage build: "builder" installs dependencies, "runtime" holds only what runs.

FROM python:3.13-slim AS builder
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock requirements-build.lock ./
# Hash-checked install: every dependency must match the lockfile exactly.
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --require-hashes -r requirements.lock -r requirements-build.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps --no-build-isolation .

FROM python:3.13-slim AS runtime
ARG GIT_SHA=unknown
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYDANTIC_AI_NO_BANNER=1 \
    AGENT_LAB_PROVIDER=mock \
    AGENT_LAB_RUN_LOG_DIR=/data/runs \
    AGENT_LAB_GIT_SHA=$GIT_SHA
RUN useradd --uid 10001 --create-home agent && mkdir -p /data/runs && chown -R agent /data
COPY --from=builder /opt/venv /opt/venv
USER 10001
WORKDIR /home/agent
VOLUME ["/data"]
ENTRYPOINT ["agent-lab"]
CMD ["run", "What is (12+30)*2 and what is docker?"]
