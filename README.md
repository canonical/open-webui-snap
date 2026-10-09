# Open WebUI snap

This repository contains the snap packaging configurations for Open WebUI.

Open WebUI itself is included into the snap as a Python pip package.

## Auto configuration via snap connections

Open WebUI supports automatic configuration of OpenAI-compatible endpoints through the snap [content interface](https://snapcraft.io/docs/content-interface). Inference snaps such as Gemma 4 expose a `provider` slot that Open WebUI reads to register the endpoint automatically. Ollama discovery is stubbed out until the provider interface supports it; manually configured Ollama connections are preserved.

### Connecting a snap

```
sudo snap connect open-webui:provider <snap-name>:provider
```

After the connection is made, Open WebUI's `connect-plug-provider` hook runs immediately and adds the endpoint to the database. If Open WebUI was still initialising when the hook ran, a periodic job re-checks and applies any pending configs once every minute.

To disconnect:

```
sudo snap disconnect open-webui:provider <snap-name>:provider
```

The periodic job detects the disconnection and removes the endpoint from the database within one minute.

### Providing a provider slot

To expose a `provider` slot that Open WebUI can consume, declare a slot with content ID `inference-provider` in your snap's `snapcraft.yaml`:

```yaml
slots:
  provider:
    interface: content
    content: inference-provider
    source:
      read:
        - $SNAP_COMMON/share/provider
```

Inside the shared directory (`$SNAP_COMMON/share/provider`), publish `provider.env` in the literal `KEY=value` format produced by `modelctl run --share-provider`:

```dotenv
SNAP_NAME=gemma4
SNAP_INSTANCE_NAME=gemma4
OPENAI_BASE_URL=http://127.0.0.1:8081/v1
```

Snapd mounts each shared provider directory under `$SNAP/shared-configs/`
(for example, `provider/` and `provider-2/` for multiple connections).
Open WebUI reads `OPENAI_BASE_URL` without executing the file and registers it
as an unauthenticated, snap-tagged OpenAI connection. Metadata keys are ignored.
The importer no longer reads the legacy `openai.json` or `ollama.json` files.
