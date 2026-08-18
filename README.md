# Open WebUI snap

This repository contains the snap packaging configurations for Open WebUI.

Open WebUI itself is included into the snap as a Python pip package.

## Automatic inference-snap discovery

Open WebUI automatically discovers local inference snaps (such as
[gemma4](https://snapcraft.io/gemma4)) that expose an OpenAI-compatible API on
localhost. This is handled by a bundled plugin, **Snap Model Auto-Discovery**,
which is seeded into Open WebUI on install and enabled by default.

### How it works

On each model listing, the plugin scans a range of local ports for an
OpenAI-compatible `/v1` or `/v3` endpoint and registers any models it finds. No
snap interface connection or manual configuration is required — simply install
an inference snap alongside Open WebUI:

```
sudo snap install open-webui
sudo snap install gemma4
```

The discovered models appear in Open WebUI automatically once the inference
snap's server is up. Stopping or removing the inference snap makes its models
disappear again on the next refresh.

### Configuration

The plugin's behaviour can be tuned from Open WebUI under
**Admin Settings → Functions → Snap Model Auto-Discovery** (valves):

* `PORT_RANGES` – comma-separated list of ports/ranges to scan
  (default `8330-8340`).
* `DUMMY_API_KEY` – placeholder API key sent to the local endpoints
  (default `sk-local-snap`).

The plugin source lives in
[`plugins/inference-snaps-plugin.py`](plugins/inference-snaps-plugin.py) and is
seeded by [`scripts/seed-plugins.py`](scripts/seed-plugins.py).

### Upgrading from interface-based versions

Earlier releases registered inference snaps through a snap **content interface**
(`open-webui:config`) and a periodic job that wrote the endpoint into Open
WebUI's database, tagging it with `snap`. The auto-discovery plugin replaces
that mechanism entirely; the `config` plug and the import job have been removed.

When upgrading from one of those older releases, the previously imported,
`snap`-tagged OpenAI/Ollama connection is **left in the database as-is** — the
upgrade intentionally does not modify your connections. As a result you may see
the same model listed twice: once from the leftover connection and once from the
plugin.

To resolve this, open **Admin Settings → Connections** and delete the stale
`snap`-tagged connection. The plugin will continue to surface the model on its
own. New (fresh) installs are unaffected.
