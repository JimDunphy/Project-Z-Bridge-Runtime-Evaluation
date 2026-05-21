# Project Z-Bridge Runtime Evaluation

This is a runtime-only Project Z-Bridge package for partner technical
evaluation. It does not include Rust source code.

Use is limited by `LICENSE.txt`: no production use, redistribution, resale,
sublicensing, or inclusion in another product without a separate written
agreement.

Requires Docker 20.10 or newer and Docker Compose v2. Standalone
`docker-compose` 1.29.x or newer is accepted as a fallback.

## Required Steps

1. Create and edit the local environment file.

   ```sh
   cp env .env
   vi .env
   ```

   Edit the `BEGIN CHANGE ME` / `END CHANGE ME` block in `.env`.

   ```text
   STALWART_BASE_URL=https://stalwart.example.com
   BRIDGE_PUBLIC_BASE_URL=https://bridge.example.com
   BRIDGE_IMAGE_PROXY_SECRET=LEopkKgMoB2p8fu4dovSKtWIPGT0whgI9zSntWxghi7CpuMTfcgqnJRyBK4DV7L-
   BRIDGE_EMAIL_IFRAME_RENDERER_ORIGIN=https://renderer.example.com
   ```

   Note: For a quick test without TLS certificates, hostnames, or a reverse
   proxy, see `Direct HTTP Test Without Nginx` in `RUNTIME_GUIDE.md`.

2. Initialize the runtime one time to extract ZWC static assets.

   ```sh
   ./build.sh init
   ```

3. Start the bridge.

   ```sh
   ./build.sh start
   ```

4. Check health.

   ```sh
   ./build.sh health
   ```

5. Open the reference ZWC client.

   ```text
   http://127.0.0.1:${BRIDGE_PORT}/zimbra/
   ```

## Updating

When a new bridge image is published:

```sh
./build.sh update-image
./build.sh restart
```

`update-image` updates the runtime checkout first, then updates only the bridge
image. It does not modify `.env`, `static/zimbra/`, `data/`, or nginx
configuration.

## More Information

Read `RUNTIME_GUIDE.md` for update options, reverse proxy notes, asset import
details, runtime state, and compatibility boundaries.

Use `.env.example` only as a broader environment reference. Additional Project
Z-Bridge documentation is available at:

```text
https://github.com/JimDunphy/Project-Z-Bridge-PreRelease
```
