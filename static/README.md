# Static Assets

`static/zimbra/` is intentionally empty in the runtime package.

By default, initialization downloads the supported classic ZWC `zimbra.war`
release asset and extracts it here:

```sh
./build.sh init
```

The default release asset is:

```text
https://github.com/JimDunphy/DockerZimbraRHEL8/releases/latest/download/zimbra.war
```

To import a local WAR instead:

```sh
./build.sh init --assets /path/to/zimbra.war
```

Supported inputs:

- `zimbra.war`
- `.zip`
- `.tar.gz` / `.tgz`
- an already extracted ZWC directory

The runtime mounts `./static/zimbra` read-only into the container at
`/webclient`.
