# Docker concepts used in Agent Lab

Each section says what the concept is, where Agent Lab uses it, and why.

### Image vs. container
An **image** is a read-only, layered filesystem plus metadata such as the default command.
A **container** is a running process started from an image, with its own thin writable layer.
`docker build` makes images; `docker run` makes containers. You can start many containers from one image.

### Dockerfile and layers
The `Dockerfile` is the recipe. Each instruction (`FROM`, `COPY`, `RUN`, `ENV`) adds a **layer**.
Docker caches layers and rebuilds only from the first changed instruction onward.
That's why we `COPY pyproject.toml` before `src`: a dependency layer that rarely changes should come early.

### Base image (`FROM python:3.13-slim`)
This is the starting filesystem. `slim` is Debian with a minimal Python, much smaller than the full image.
For reproducibility you can pin it further by digest (`python:3.13-slim@sha256:...`).

### Multi-stage build (`AS builder`, `AS runtime`)
The first stage installs dependencies into `/opt/venv`. The second stage starts clean and copies
only that venv: `COPY --from=builder`. Build leftovers such as pip caches and source files
never reach the final image, so it is smaller and has less attack surface.

### `ENV`, `ENTRYPOINT`, `CMD`
- `ENV` sets environment variables. This app reads all of its configuration from them.
- `ENTRYPOINT ["agent-lab"]` is the fixed executable.
- `CMD [...]` gives default arguments, which anything after the image name in `docker run` replaces:
  `docker run agent-lab:dev healthcheck`.

### Non-root user (`USER 10001`)
A process in a container runs as root by default. Running it as an unprivileged UID limits the damage
a compromised process can do. Kubernetes enforces the same rule with `runAsNonRoot`.

### `.dockerignore`
This file keeps `.git`, `.venv`, `.env` and similar paths out of the **build context**: the files sent to the builder.
Builds get faster, and secrets can't be copied into an image by accident.

### Volumes (`VOLUME /data`, named volume `runs`)
A container's writable layer is deleted together with the container. A **named volume** is storage that
Docker manages and that outlives containers, so `runs.jsonl` survives `docker compose run --rm`.
A **bind mount** (`./:/app:ro` in the `test` service) maps a host folder into the container,
so the tests see your live source code.

### Hardening flags
- `read_only: true` makes the root filesystem read-only. Only the volume and the `tmpfs` at `/tmp` are writable.
- `cap_drop: [ALL]` removes Linux capabilities.
- `no-new-privileges` blocks privilege escalation through setuid binaries.
- `--network none`, used in CI, proves the mock path needs no network at all.

### Docker Compose (`compose.yaml`)
Compose describes a set of containers (**services**) declaratively, so you don't type long
`docker run` commands. `env_file` loads `.env` (optional here). `profiles: [test]` keeps the
`test` service from starting unless you ask for it. `docker compose run --rm <svc> <args>` runs a
one-off container and removes it afterwards.

### Registries (not used yet)
Images can be pushed to a registry, such as GitHub Container Registry, which is free for
private images within your plan's limits. In Phase 1 images stay local:
k3d *imports* them straight into the cluster node.
