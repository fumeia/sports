# Shared Obsidian vault (Self-hosted LiveSync)

You and your collaborators each use the real Obsidian app on your own
devices. A shared vault is synced through your server: CouchDB in Docker,
reachable only through Caddy (HTTPS). Changes arrive within seconds.

- **You** manage it with the `admin` login (kept in `/opt/livesync/.env`).
- **Each collaborator** gets their own login and can read and write the
  vault's notes and images. They can't manage the database, and they can't
  touch your plugins or settings: the plugin syncs notes, not settings,
  unless someone switches that on.
- **Nightly backup** at 03:30 to `/var/backups/livesync/` (14 days kept),
  since anyone with access can also delete notes.

## Install (server)

1. At Spaceship add an A record: `sync` → your server's IP (or the
   subdomain you want).
2. Set up the sync service (asks the subdomain and domain, then `yes`):
   ```
   bash /opt/vb/livesync/setup.sh
   ```
3. Add the line it shows to `/etc/caddy/Caddyfile`
   (`import /etc/caddy/livesync.caddy`), then check and reload:
   ```
   caddy validate --config /etc/caddy/Caddyfile
   ```
   ```
   systemctl reload caddy
   ```
4. A login for yourself (use this in your Obsidian apps; keep `admin` for
   managing), and one per collaborator:
   ```
   bash /opt/vb/livesync/add-user.sh yourname
   ```
   ```
   bash /opt/vb/livesync/add-user.sh anna
   ```
   Each prints the four values to enter in Obsidian. Send them privately.
   To take someone's access away: `bash /opt/vb/livesync/remove-user.sh anna`

## Connect Obsidian (each person, each device)

1. Obsidian → Settings → Community plugins → Browse → **Self-hosted
   LiveSync** → Install → Enable.
2. In its settings, choose the manual setup and enter **Server URI**,
   **Username**, **Password** and **Database** (`vault`). Test the
   connection.
3. **The first device** (yours, the one that already has your notes)
   sends its notes to the server. **Every other device** fetches from the
   server. The plugin asks which it is.
4. Leave the plugin's options for syncing settings, plugins and hidden
   files **off**, so nobody's plugins end up on anyone else's device.
5. End-to-end encryption is optional (the server is yours). If you switch
   it on, every device needs the same passphrase.

Your browser Obsidian at `blog.<domain>` can be one of your devices: install
the plugin there too, with your own login.
