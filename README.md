# Open WebUI snap

This repository contains the snap packaging configurations for Open WebUI.

Open WebUI itself is included into the snap as a Python pip package.

## Automatic inference snap discovery

The Open WebUI snap ships with a plugin that automatically discovers inference snaps on the system
and registers them with Open WebUI.
This allows you to use inference snaps without any manual configuration.

### Upgrading from interface-based versions

Older releases registered inference snaps via a content interface, leaving a
`snap`-tagged connection in the database. Upgrades do not remove it, so a model
may appear twice. To fix this, delete the stale `snap`-tagged connection under
**Admin Settings → Connections**. Fresh installations are unaffected.
