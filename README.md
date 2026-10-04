# grinning-cat-swarm

A standalone Docker Compose stack that runs **N replicas** of
[grinning-cat-core](https://github.com/Grinning-Cat-RAG/grinning-cat-core) behind **Traefik**, sharing a
**single Redis** and a **single Qdrant**.

```
                         ┌──────────────────────┐
  clients ──► :1865 ───► │       traefik        │  load balancer (Docker provider)
                         └──────────┬───────────┘
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
     grinning-cat-core-1   grinning-cat-core-2   …   grinning-cat-core-N
              │  shared volumes: plugins_data, data, venv_shared  │
              └──────────────┬──────────────────────┬─────────────┘
                             ▼                      ▼
                    grinning-cat-redis    grinning-cat-vector-memory (Qdrant)
```

## Contents

| File | Purpose |
|---|---|
| `compose.yml` | The whole stack: Traefik, the one-shot bootstrap, the replicas, Redis, Qdrant. |
| `bootstrap.py` | One-shot init (shared venv, volume ownership, migrations) and entrypoint of the replicas. |
| `.env.example` | Template of `.env`: stack settings and the configuration shared by every replica. |
| `tests/` | Tests of `bootstrap.py` (unit + stateful, property-based with fault injection). |

## Requirements

- Docker Engine with Docker Compose **v2.24+** (optional `env_file` entries).
- A **single Docker host**: the replicas share named volumes and coordinate through `flock` on them, which does not
  work across hosts.
- A grinning-cat-core image: the standard one (`Dockerfile`, runs as root) or the hardened one (`Dockerfile:dhi`, runs
  as the unprivileged `nonroot` user). The stack does not depend on the image's user nor on its entrypoint:
  `bootstrap.py` replaces the entrypoint, and the replicas run as the image's user.
- The stack runs **from a checkout of this repository**: `bootstrap.py` is mounted from it. If it is missing, Docker
  mounts an empty directory in its place (and creates it on the host, owned by root): the containers stop with
  `bootstrap.py not found`.

## Quick start

```bash
cp .env.example .env
# set at least CAT_API_KEY and CAT_JWT_SECRET (>= 32 characters), plus CAT_CRYPTO_KEY / CAT_CRYPTO_SALT
docker compose up -d --wait
```

The API is published by Traefik on `http://localhost:1865` (`CAT_PUBLIC_PORT`), WebSockets included.

### Scaling

```bash
CAT_REPLICAS=5 docker compose up -d --wait               # or set CAT_REPLICAS in .env
docker compose up -d --wait --scale grinning-cat-core=5   # same effect, --scale wins over CAT_REPLICAS
```

Traefik discovers the replicas through the Docker socket and routes only to the healthy ones (the container
healthcheck probes `/health/liveness`). New replicas join the pool as soon as they become healthy. No sticky sessions
are needed: a WebSocket message produced on any replica reaches the replica holding the connection through Redis
pub/sub.

## Configuration

`.env` is read twice by Docker Compose: to interpolate `compose.yml`, and as `env_file` of the bootstrap and of every
replica, so **all the replicas run with the same configuration** — this matters for values that must match across
replicas, like `CAT_JWT_SECRET`, `CAT_CRYPTO_KEY` and `CAT_CRYPTO_SALT`.

| Variable | Default | Meaning |
|---|---|---|
| `CAT_IMAGE` | `ghcr.io/grinning-cat-rag/grinning-cat-core:latest` | Image run by the bootstrap and by the replicas (standard or hardened). |
| `CAT_REPLICAS` | `3` | Number of replicas. |
| `CAT_PUBLIC_PORT` | `1865` | Host port of Traefik. |
| `CAT_LOG_LEVEL` | `INFO` | Log level of the replicas. |
| `CAT_TRUSTED_PROXIES` | — (none) | Comma-separated IPs/CIDRs, without spaces, of the proxies in front of Traefik (load balancer, TLS terminator) whose `X-Forwarded-*` headers Traefik keeps. See [Behind another proxy](#behind-another-proxy). |
| `REDIS_IMAGE` | `redis/redis-stack-server:7.4.0-v8` | Redis image. |
| `QDRANT_IMAGE` | `qdrant/qdrant:v1.19.1` | Qdrant image. |
| `TRAEFIK_IMAGE` | `traefik:v3.7.13` | Traefik image. |
| `CAT_API_KEY` | — (required) | Access token of the API. |
| `CAT_JWT_SECRET` | — (required) | JWT signing secret, at least 32 characters. |

Every other `CAT_*` setting of the core (see `.env.example`) can be set in `.env`.

Redis, Qdrant and Traefik are pinned to exact versions: a `docker compose pull` never upgrades them by surprise —
Redis and Qdrant may convert their storage on a new version, with no way back. Upgrade them on purpose, by changing
`REDIS_IMAGE`, `QDRANT_IMAGE` and `TRAEFIK_IMAGE`.

Set by `compose.yml`, these win over `.env`:

| Variable | Value | Why |
|---|---|---|
| `CAT_REDIS_HOST` / `CAT_REDIS_PORT` | `grinning-cat-redis` / `6379` | The single Redis of the stack. |
| `CAT_QDRANT_HOST` | `grinning-cat-vector-memory` | The single Qdrant of the stack. |
| `CAT_DEBUG` | `false` | The core's default (`true`) runs uvicorn with `--reload`. |
| `CAT_HTTPS_PROXY_MODE` | `true` | uvicorn trusts the `X-Forwarded-*` headers set by Traefik: rate limiting sees the real client IP, and the cookie Origin check sees the public host. |
| `CAT_CORS_FORWARDED_ALLOW_IPS` | `*` | Traefik's IP is not fixed. The replicas publish no port, so only the containers of the stack's network can reach them. |

### Behind another proxy

Traefik discards the `X-Forwarded-*` headers it receives, and sets them from the address of whoever connects to it.
With clients connecting straight to Traefik this is what protects the core from spoofed client IPs. With another proxy
in front of Traefik, though, every request comes from that proxy: the core sees a single client IP, so the login rate
limit (30 attempts per IP every 15 minutes, by default) becomes a limit shared by all the users. List that proxy in
`CAT_TRUSTED_PROXIES` (e.g. `CAT_TRUSTED_PROXIES=10.0.0.5` or `10.0.0.0/8`; several values comma-separated, without
spaces), so that Traefik keeps its headers and the core sees the real client IP.

That proxy must **overwrite** `X-Forwarded-For` with the address of its client, not append to the one it receives: the
core takes the leftmost address of the header, and with an appending proxy that is whatever the client sent — a
client could then pick its own IP and dodge the login rate limit. (nginx: `proxy_set_header X-Forwarded-For
$remote_addr;` rather than `$proxy_add_x_forwarded_for`.)

## How the stack starts

1. **Redis** and **Qdrant** start and become healthy.
2. **`grinning-cat-bootstrap`** runs once, with the same image as the replicas, as root:
   1. it **seeds the shared venv** (see below);
   2. it gives the shared venv, plugins and data volumes to the **image's user** (the owner of the image's venv: root
      in the standard image, `nonroot` in the hardened one) — the whole reseeded venv, and the plugins and data
      volumes when their root belongs to someone else, e.g. after switching from one image to the other;
   3. it applies the **Redis migrations**, once, before any replica starts (the replicas do not run them); then the
      container exits with 0.
3. The **replicas** start only after the bootstrap exits with 0, as the image's user, through
   `bootstrap.py exec -- python -m cat.main`: it takes the shared lock on the venv (see below), then becomes the Cat.
   Each one finds the plugins on the shared plugins
   volume, reads from Redis which ones are active and activates them, installing their dependencies when needed (see
   [Plugin dependencies](#plugin-dependencies)).
4. **Traefik** adds each replica to the pool once its healthcheck passes.

Both the bootstrap and the replicas run with `init: true`: PID 1 of their containers is Docker's `tini`
(`docker-init`), which starts the entrypoint (`sh`, which execs `bootstrap.py`; in the replicas `bootstrap.py` then
execs the Cat). `tini` forwards `SIGTERM` to it, so `docker compose stop` stops a replica at once instead of killing it
after the stop timeout, and reaps orphaned processes left as zombies (e.g. by the subprocesses spawned while
installing plugin dependencies).

### Shared state

| Volume | Mounted at | Shared because |
|---|---|---|
| `plugins_data` | `/app/cat/plugins` | A plugin installed through one replica must exist on all of them; it also holds each plugin's install lock and installation record (`.install.lock`, `.requirements_hash`, `uv.lock`). |
| `venv_shared` | `/app/.venv` | Plugin dependencies are installed once, by one replica, for all of them (see below). |
| `data` | `/app/data` | Files of the local file manager (`/app/data/storage`). |
| `redis_data` | Redis `/data` | Settings, users, active plugins, chat history, plugin events, WebSocket fan-out. |
| `qdrant_data` | Qdrant `/qdrant/storage` | Vector memory. |

Each replica keeps its own identity (`/app/.pod_id`, in the container's own filesystem), used to tell its own plugin
events apart from the others'.

### Why a shared venv, and what `bootstrap.py` does

The core records that a plugin's dependencies are installed by writing `.requirements_hash` in the plugin's folder,
which lives on the shared plugins volume. Once any replica has written it, every other replica trusts it and installs
nothing. The dependencies must therefore be in a venv **every replica sees**: `/app/.venv` is the `venv_shared`
volume.

Docker copies an image's content into a named volume only while the volume is empty, so after an image upgrade the
replicas would keep running on the **old image's venv**. `bootstrap.py` prevents that. On every start it compares a
fingerprint of the image's venv (installed distributions + Python version) with the one recorded in the shared venv
(`.grinning-cat-seed`); when they differ it:

1. removes the record, so an interrupted run is redone at the next start;
2. empties the shared venv and copies the image's venv into it;
3. removes the `.requirements_hash` of every installed plugin, because their dependencies lived in the replaced venv —
   the replicas reinstall them when they activate the plugins;
4. gives the whole shared venv to the image's user;
5. writes the new record: it certifies a complete copy, with its ownership.

When the fingerprints match it changes nothing, so the dependencies installed by the replicas are kept. The
fingerprint sees the names and versions of the installed distributions, not their content: an image rebuilt with the
same versions but different files (e.g. a locally patched package) does not trigger a reseed. To force one, remove the
volume: `docker compose down`, then `docker volume rm <project>_venv_shared`. The replicas
mount the volume with `nocopy`: only the bootstrap seeds it.

The venv is never replaced under a running replica. Each replica holds a **shared** `flock` on
`/app/.venv/.grinning-cat-seed.lock` for its whole life (`bootstrap.py exec` takes it, waiting for a reseed in progress,
and the Cat, which it execs, inherits it). A replica refuses to start on a venv whose reseed was interrupted (no `.grinning-cat-seed`):
run the bootstrap again (`docker compose up -d`). The bootstrap checks the record under a shared lock too, and reseeds only if it can take the
**exclusive** lock at once; otherwise it exits with 1, asking to stop the replicas first
(`docker compose stop grinning-cat-core`). This also covers a replica that is installing a plugin's dependencies
while the bootstrap runs.

## Plugins

### What a plugin package is

A plugin is distributed as an **archive**: a `.zip` or a `.tar` file (the type is guessed from the file name's
extension; anything else is rejected). Its content is the plugin's folder, either at the root of the archive or inside
a single top-level folder:

```
my_plugin.zip                       my_plugin.zip
├── plugin.json                     └── my_plugin/            ← a single top-level folder is unwrapped
├── pyproject.toml          or          ├── plugin.json
├── my_hooks.py                         ├── pyproject.toml
└── tools/                              └── my_hooks.py
    └── my_tools.py
```

- **`*.py`** — the plugin's code (hooks, tools, forms, endpoints), at any depth. At least one Python file is required.
  Every Python file is scanned (AST) before installation: a file judged unsafe, or one that cannot be parsed, rejects
  the whole package.
- **`plugin.json`** — optional manifest: `name`, `description`, `version`, `author_name`, `author_url`, `plugin_url`,
  `tags`, `thumb`, `min_cat_version`, `max_cat_version`, and `dependencies`, the ids of **other plugins** it requires.
- **`pyproject.toml`** — optional: the plugin's **Python** dependencies, in `[project].dependencies`.
  `requirements.txt` is **not** supported: it is ignored.

```toml
[project]
name = "my-plugin"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["pyfiglet==1.0.2"]
```

The **plugin id** is the archive's **file name** without the extension, slugified with `_`: `My Plugin-v2.zip` becomes
`my_plugin_v2`, whatever the name of the folder inside. The id is the plugin's folder name on the shared volume, so
uploading an archive with the same file name again **replaces** the installed plugin (an update).

### Where the package goes

You do not copy the archive anywhere: you **send it to the API**, through Traefik, and the replica that receives it
installs it on the shared plugins volume (`plugins_data`, mounted at `/app/cat/plugins` in every replica) for the whole
stack. Two sources are supported:

- **Upload** of a local archive (permission `SYSTEM` / `WRITE`):

  ```bash
  curl -X POST http://localhost:1865/plugins/install/upload \
       -H "Authorization: Bearer $CAT_API_KEY" \
       -F "file=@my_plugin.zip"
  ```

  The receiving replica saves the upload in its own temporary folder (`/tmp/<file name>`, inside its container), and
  deletes it once extracted.

- **Registry**: the core asks the public plugin registry to download a plugin by its URL (typically its repository):

  ```bash
  curl -X POST http://localhost:1865/plugins/install/registry \
       -H "Authorization: Bearer $CAT_API_KEY" -H "Content-Type: application/json" \
       -d '{"url": "https://github.com/plugin-dev-account/plugin-repo"}'
  ```

  The registry returns a zip, saved as `/tmp/<last segment of the URL>.zip` in the receiving replica: the plugin id
  derives from the last segment of the URL (`plugin_repo` here).

Both calls answer immediately (`"Plugin is being installed asynchronously"`); the installation goes on in the
background.

### Hot installation, step by step

Traefik delivers the request to **one** replica — call it A.

1. **A answers** and goes on in a background task.
2. **A extracts the archive** into a temporary folder, unwraps the single top-level folder if there is one, scans the
   Python files, then moves the content to `/app/cat/plugins/<plugin_id>` — the shared volume: from now on the plugin's
   files are visible to every replica. An existing folder with the same id is replaced. The archive is deleted.
3. **A checks the plugin dependencies** declared in `plugin.json`: if a required plugin is not installed, A removes
   the folder and the installation fails.
4. **A activates the plugin**: it installs the plugin's Python dependencies into the shared venv (see
   [Plugin dependencies](#plugin-dependencies)), imports its modules, registers its hooks, tools and forms, mounts its
   endpoints on its own API, and re-activates it in the agents that already had it enabled (an update). The plugin is
   added to the active plugins stored in Redis.
5. **A publishes a `plugin_installation` event** on the Redis stream `march_hare:streams:plugin_events`, tagged with
   A's pod id.
6. **Every other replica** (B, C, …) listens on that stream through the core plugin *March Hare*. On the event — events
   tagged with its own pod id are ignored — each one activates the plugin from the shared volume: its dependencies are
   already in the shared venv and recorded by `.requirements_hash`, so **nothing is downloaded or installed again**;
   the replica imports the modules from the shared venv, registers hooks, tools and forms, and mounts the plugin's
   endpoints on its own API.

From then on, a request routed to any replica finds the plugin. Because the API answers before the installation ends,
poll `GET /plugins` (or call one of the plugin's endpoints) to know when it is ready; errors appear only in the logs of
replica A (`docker compose logs grinning-cat-core`).

Replicas that are not running when the event is published do not need it: they listen to the stream from the moment
they start, and at startup a replica activates every active plugin found on the shared volume. The same holds for the
replicas added later with `--scale`.

### Uninstallation

```bash
curl -X DELETE http://localhost:1865/plugins/uninstall/my_plugin -H "Authorization: Bearer $CAT_API_KEY"
```

The receiving replica deactivates the plugin, removes its folder from the shared volume and publishes a
`plugin_uninstallation` event on the same stream, which the other replicas receive. The plugin's Python dependencies
stay in the shared venv (the core never uninstalls packages) until the next reseed of the venv.

### Copying a plugin folder by hand

A plugin folder can also be copied straight onto the shared volume, e.g. through any running replica:

```bash
docker compose cp -a ./my_plugin grinning-cat-core:/app/cat/plugins/my_plugin
```

`-a` gives the copied files to the image's user: without it they belong to root, and with the hardened image the
replicas could not write in the plugin's folder (install lock, `uv.lock`, `.requirements_hash`).

No replica is notified of it, and a plugin found this way is not active: the replicas activate, at startup, only the
plugins listed as active in Redis. Activating it with `PUT /plugins/system/toggle/my_plugin` acts only on the replica
that receives the call (toggling is not propagated to the other replicas), though it updates the list in Redis: restart
the replicas (`docker compose restart grinning-cat-core`) to have all of them activate it. Prefer the API upload: it is
the only path that installs and activates a plugin on all the running replicas at once.

### Plugin dependencies

When a replica activates a plugin whose `pyproject.toml` declares dependencies, the core:

1. computes a hash of the plugin's `pyproject.toml` together with the core's `uv.lock`; if it equals the plugin's
   `.requirements_hash`, the dependencies are already installed and nothing else happens;
2. otherwise takes a non-blocking `flock` on the plugin's `.install.lock` — on the shared volume, so it is seen by all
   the replicas:
   - the replica that gets the lock installs;
   - the others poll for up to 6 seconds for the hash to appear; if it does not, they wait for the lock and check the
     hash again once they get it, so the installation still happens once;
3. compiles the plugin's `pyproject.toml` into a fresh `uv.lock`, adding as `constraint-dependencies` every package
   pinned by the core's `uv.lock`: a plugin can never upgrade or downgrade a library the core depends on — an
   incompatible plugin fails to resolve instead;
4. installs strictly from that lock into `/app/.venv` (`uv sync --active --frozen --inexact`: packages unrelated to the
   plugin are left untouched). The packages are downloaded from PyPI, so the replicas need Internet access;
5. stores the compiled `uv.lock` next to the plugin's `pyproject.toml` and writes `.requirements_hash`.

When N replicas start together, one installs and the others wait for it. After an image upgrade the bootstrap has
removed every `.requirements_hash`, so the first replica that activates a plugin reinstalls its dependencies into the
freshly seeded venv.

## Upgrading

```bash
docker compose pull          # or change CAT_IMAGE
docker compose up -d --wait
```

Compose stops the old replicas, runs the bootstrap and starts the new replicas. The bootstrap reseeds the shared venv
if the image's venv changed and marks the plugins for reinstallation; the replicas reinstall the plugin dependencies on
activation, before answering (their healthcheck allows up to 10 minutes for it). Meanwhile the stack is unavailable:
the replicas are replaced, not rolled.

Running the bootstrap alone while the replicas are up (`docker compose run grinning-cat-bootstrap`,
`docker compose up -d grinning-cat-bootstrap`) after changing the image fails on purpose: stop the replicas first.

### Rolling back

The venv side of a rollback works like an upgrade: the older image's fingerprint differs from the recorded one, so the
venv is reseeded from it. **The Redis migrations do not roll back by themselves.** If the newer image added migrations,
the older one does not know them, and its bootstrap would apply all of its own migrations again over the migrated
data. Before switching back, downgrade Redis with the **newer** image, to the head revision of the older one:

```bash
docker compose stop grinning-cat-core
docker compose run --rm --no-deps --entrypoint python grinning-cat-bootstrap \
    migrations/manage_migrations.py downgrade <head revision of the older image>
# then set CAT_IMAGE to the older image
docker compose up -d --wait
```

`manage_migrations.py heads`, run with the older image, prints its head revision.

## Development

```bash
uv run pytest --cov --cov-report=term-missing
```

The tests cover `bootstrap.py` with full branch coverage, plus a stateful property-based test (Hypothesis) that
interleaves image releases and rollbacks, replicas starting and stopping, plugin installations by the replicas and
bootstrap runs — interrupted at every step, migrations included — and checks after each step that:

- the shared venv is declared seeded only if it holds the whole image's venv;
- while it is declared seeded, every plugin recorded as installed has its dependencies in it;
- a successful run always leaves it seeded for the current image;
- a run on an up-to-date venv changes nothing;
- the lock file survives every run;
- while a replica runs, the bootstrap touches neither the shared venv nor the plugin records;
- a run succeeds only if it applied the migrations, and never migrates when the venv could not be brought up to date;
- while the venv is declared seeded, all of it belongs to the image's user, even after an interrupted ownership change;
- after a run that succeeds, the plugins and data volumes belong to the image's user too.
