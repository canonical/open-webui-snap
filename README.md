# Open WebUI snap

This repository contains the snap packaging configurations for Open WebUI.

Open WebUI itself is included into the snap as a Python pip package.

## Auto configuration via snap connections

Open WebUI supports automatic configuration of OpenAI-compatible and Ollama endpoints through the snap [content interface](https://snapcraft.io/docs/content-interface). Inference snaps (e.g. snaps that serve an LLM) can expose a `config` slot that Open WebUI reads to register the endpoint automatically.

### Connecting an inference snap

```
sudo snap connect open-webui:config <inference-snap>:open-webui
```

After the connection is made, Open WebUI's `connect-plug-config` hook runs immediately and adds the endpoint to the database. If Open WebUI was still initialising when the hook ran, a periodic job re-checks and applies any pending configs once every minute.

To disconnect:

```
sudo snap disconnect open-webui:config <inference-snap>:open-webui
```

The periodic job detects the disconnection and removes the endpoint from the database within one minute.

### Providing a config slot (for inference snap developers)

To expose a `config` slot that Open WebUI can consume, declare a slot with content ID `open-webui-config` in your snap's `snapcraft.yaml`:

```yaml
slots:
  open-webui:
    interface: content
    content: open-webui-config
    read:
      - $SNAP/open-webui-config
```

Inside the shared directory (`$SNAP/open-webui-config`) create one or both of the following files:

**`openai.json`** – registers an OpenAI-compatible endpoint:

```json
{
  "base_url": "http://localhost:11435/v1"
}
```

**`ollama.json`** – registers an Ollama endpoint:

```json
{
  "base_url": "http://localhost:11435"
}
```

The `base_url` field is required. Open WebUI will add the endpoint to its database tagged as `snap`-managed so that it can be cleanly removed when the interface is disconnected.
