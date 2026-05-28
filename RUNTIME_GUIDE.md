# Project Z-Bridge Runtime Guide

This guide contains the operational details for the runtime-only Project
Z-Bridge evaluation package.

## Package Contents

```text
build.sh
docker-compose.yml
env
.env.example
.gitignore
LICENSE.txt
NOTICE.txt
README.md
RUNTIME_GUIDE.md
SHA256SUMS
VERSION
dist/project-z-bridge-runtime.image.tar.gz
examples/nginx/project-z-bridge-runtime-eval.conf
static/zimbra/
data/
```

`env` is the minimal evaluation starter config. `.env` is local deployment
state and is ignored by git.

## Environment Files

Create `.env` from `env`:

```sh
cp env .env
vi .env
```

Required minimum:

```text
STALWART_BASE_URL=https://stalwart.example.com
BRIDGE_PUBLIC_BASE_URL=https://bridge.example.com
BRIDGE_IMAGE_PROXY_SECRET=LEopkKgMoB2p8fu4dovSKtWIPGT0whgI9zSntWxghi7CpuMTfcgqnJRyBK4DV7L-
BRIDGE_EMAIL_IFRAME_RENDERER_ORIGIN=https://renderer.example.com
```

The `env` file includes a marked `BEGIN CHANGE ME` / `END CHANGE ME` block for
deployment-specific values. Values outside that block are the evaluation
profile currently being tested.

`.env.example` is included as a broader option reference.

## AI Runner Note

The evaluation config includes the AI runner settings, but AI features require
additional host-side setup. The runtime container expects an external runner at
`BRIDGE_AI_RUNNER_URL`; provider tools such as Codex CLI, Gemini, and Claude run
outside the container and need their normal local authentication or API keys.

AI setup is optional for validating the mail bridge and reference ZWC flow.

## Static Zimbra Web Client Assets

The runtime package does not include classic Zimbra Web Client (ZWC) static
assets.

The standard initialization command downloads the supported `zimbra.war` and
extracts it into `static/zimbra/`:

```sh
./build.sh init
```

The default WAR source is:

```text
https://github.com/JimDunphy/DockerZimbraRHEL8/releases/latest/download/zimbra.war
```

To import a local WAR or already extracted ZWC directory:

```sh
./build.sh init --assets /path/to/zimbra.war
./build.sh init --assets /path/to/extracted-zimbra
```

## Commands

Docker 20.10 or newer and Docker Compose v2 are recommended. Standalone
`docker-compose` 1.29.x or newer is accepted as a fallback. Older Docker or
Compose versions are not supported by this runtime package.

```text
./build.sh init [--assets PATH] [--replace-assets] [--version X.Y.Z] [--skip-assets]
./build.sh update-image [--release TAG|latest] [--repo OWNER/REPO] [--restart]
./build.sh start
./build.sh stop
./build.sh restart
./build.sh logs
./build.sh status
./build.sh health
./build.sh doctor
```

## Updating The Bridge Image

Project Z-Bridge runtime binaries are delivered as private GitHub Release
assets.

For a private GitHub repository, each machine that runs `init` or `update-image`
must be authenticated for GitHub Release asset downloads. SSH git access is
enough for `git pull`, but it is not used by GitHub's release download API.

Recommended one-time setup on the runtime host:

```sh
gh auth login
```

Token-based authentication also works:

```sh
GITHUB_TOKEN=<token> ./build.sh update-image --release v0.1.2
```

Default update:

```sh
./build.sh update-image
./build.sh restart
```

You do not need to stop the bridge before `update-image`. The running container
continues using the old image until `./build.sh restart` recreates it.

For a specific release:

```sh
./build.sh update-image --release v0.1.2
./build.sh restart
```

To download and restart in one command:

```sh
./build.sh update-image --release v0.1.2 --restart
```

By default, `update-image` first runs:

```sh
git pull --ff-only
```

If `build.sh` changes during that pull, the updated script is re-run before the
image download continues. The update command does not modify `.env`,
`static/zimbra/`, `data/`, or nginx configuration.

## Reverse Proxy Notes

The container listens on port `7777` internally. The host-published port is
controlled by `BRIDGE_PORT` in `.env`.

The default Docker mapping is:

```text
0.0.0.0:7777 -> container:7777
```

That is equivalent to this compose mapping:

```text
${BRIDGE_LISTEN_ADDR:-0.0.0.0}:${BRIDGE_PORT:-7777}:7777
```

An nginx example is included at:

```text
examples/nginx/project-z-bridge-runtime-eval.conf
```

Typical host install:

```sh
sudo cp examples/nginx/project-z-bridge-runtime-eval.conf /etc/nginx/conf.d/
sudo vi /etc/nginx/conf.d/project-z-bridge-runtime-eval.conf
sudo nginx -t
sudo systemctl reload nginx
```

Most nginx distribution packages include `/etc/nginx/conf.d/*.conf` from the
main `/etc/nginx/nginx.conf`. If this local nginx installation does not, add the
file using the site's normal include layout instead.

The example shows a main bridge origin and a separate renderer origin using one
TLS certificate whose SAN list covers both names. Replace the example
hostnames, certificate paths, and upstream target for the local deployment.

If nginx runs on the host, proxy to:

```text
http://127.0.0.1:${BRIDGE_PORT}
```

If `BRIDGE_PORT` is changed from `7777`, update the nginx upstream in
`examples/nginx/project-z-bridge-runtime-eval.conf` to match.

If nginx runs in Docker, put nginx and this service on a shared Docker network
and proxy to:

```text
http://project-z-bridge-runtime-eval:7777
```

When accessed through HTTPS, set:

```text
BRIDGE_PUBLIC_BASE_URL=https://bridge.example.com
BRIDGE_COOKIE_SECURE=1
```

## Direct HTTP Test Without Nginx

For a quick local test without TLS certificates, hostnames, or a reverse proxy,
use the Docker-published host port directly. Stalwart must still be reachable
from the container.

```text
STALWART_BASE_URL=https://stalwart.example.com
BRIDGE_PUBLIC_BASE_URL=http://127.0.0.1:${BRIDGE_PORT}/
BRIDGE_COOKIE_SECURE=0
BRIDGE_EMAIL_IFRAME_RENDERER_ENABLED=false
BRIDGE_EMAIL_IFRAME_SANDBOX_ENABLED=false
```

Use the actual port value from `.env`, for example:

```text
BRIDGE_PUBLIC_BASE_URL=http://127.0.0.1:7777/
```

Then open the reference client in a browser:

```text
http://127.0.0.1:7777/zimbra/
```

Health check:

```text
http://127.0.0.1:7777/healthz
```

Set the iframe values explicitly to `false` for this test profile instead of
commenting them out. This keeps the local test independent of the evaluation
profile defaults.

Use `BRIDGE_COOKIE_SECURE=0` for predictable plain HTTP localhost testing. Some
browsers allow `Secure` cookies on localhost or loopback addresses, but that
behavior should not be required for this evaluation path. This setting only
controls the browser cookie issued by Project Z-Bridge. It does not change the
upstream connection to Stalwart; keep `STALWART_BASE_URL=https://...` when the
Stalwart server is available over HTTPS.

## Runtime State And Moving The Bridge Host

Stalwart is the source of truth for core mailbox data. Project Z-Bridge also
keeps bridge-local compatibility state on disk.

In this runtime package, that state lives in:

```text
./data/
```

The container mounts it as `/data` and sets `BRIDGE_DATA_DIR=/data`. Keep that
mapping unchanged unless there is a specific reason to change both
`docker-compose.yml` and the runtime environment together.

This is different from the source/development checkout, where local state may
live under `.dev/`. Do not expect `.dev/` in this runtime-only package.

Bridge-local state can include ZWC-visible items such as signatures,
personas/identities, preferences, tag definitions/colors, folder metadata,
filter metadata, feed state, and performance/cache files. If `./data/` is
empty on a new machine, mail can still load from Stalwart, but those
bridge-local items may appear reset.

This section applies when moving the Project Z-Bridge runtime host. It does not
apply to ordinary browser users. Users can move between workstations without
copying anything locally as long as they keep using the same reverse-proxy URL
for the Project Z-Bridge host.

To move the bridge runtime to another host, the new host still needs:

```text
./build.sh init
updated .env for this host
reverse proxy configuration, if used
data/
```

The `data/` archive step below applies when `data/` is local disk. If `data/`
is backed by shared or distributed storage, such as a mounted distributed
filesystem, keep the mount consistent on the new bridge host instead of copying
the directory.

Archive `data/` after stopping the bridge:

```sh
./build.sh stop
tar -czf bridge-data.tar.gz data/
```

On the new host, clone or update the runtime checkout, create a fresh `.env`
from `env`, edit it for that host, initialize the Docker image and Zimbra Web
Client (ZWC) static assets, then extract the archive before starting:

```sh
cp env .env
vi .env
./build.sh init
tar -xzf bridge-data.tar.gz
./build.sh start
```

Copying `.env` directly is only appropriate when intentionally preserving the
same deployment settings. For a new host, recreate `.env` and carry over only
the values that still apply.

Copying `static/zimbra/` is optional if the new host will run `./build.sh init`
again to extract the same Zimbra Web Client (ZWC) assets.

Treat `./data/` and `.env` as private deployment state. They may contain user
preferences, signatures, cached metadata, local configuration, or secrets.

The reverse proxy should route the bridge paths needed by the reference client,
including:

```text
/zimbra/
/service/soap
/service/soap/
/service/soap/*
/service/upload
/home/*
/service/home/*
/service/image-proxy/*
/service/email-renderer/*
/public/*
/img/*
/healthz
```

## Compatibility Boundary

This runtime validates Project Z-Bridge as a Stalwart-backed mailboxd-compatible
bridge for tested ZWC/user-mail flows.

If external middleware speaks the same supported Zimbra user SOAP/REST contract,
it can be evaluated against this runtime. Product-specific APIs, admin SOAP,
private mailboxd behavior, or fields not used by the reference client are
outside this runtime validation unless separately scoped.
