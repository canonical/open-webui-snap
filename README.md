# Open WebUI snap

This repository contains the snap packaging configurations for Open WebUI.

Open WebUI itself is included into the snap as a Python pip package.

## Automatic inference snap discovery

The Open WebUI snap ships with a plugin that automatically discovers inference snaps on the system
and registers them with Open WebUI.
This allows you to use inference snaps without any manual configuration.

### Disabling the plugin

The bundled plugin is (re-)seeded from the snap on every start, so the database
always carries the version shipped with the installed revision. If you do not
want it, **disable** it under **Admin Panel → Functions** rather than deleting
it: the disabled state is preserved across refreshes, whereas a deleted
function is seeded again on the next start (which is what makes the plugin come
back if the database is reset or restored from a backup).

### Upgrading from interface-based versions

Older releases registered inference snaps via a content interface, leaving a
`snap`-tagged connection in the database. Upgrades do not remove it, so a model
may appear twice. To fix this, delete the stale `snap`-tagged connection under
**Admin Settings → Connections**. Fresh installations are unaffected.
