# Production deployment assets

The Shenzhen deployment runs the lightweight server image from GHCR. GitHub
Actions builds and tests the image; the server never compiles the project.

Files in this directory are installed as follows:

- `docker-compose.yml` -> `/opt/llmrouter/docker-compose.yml`
- `update-llmrouter` -> `/usr/local/sbin/update-llmrouter`
- `auto-update-llmrouter` -> `/usr/local/sbin/auto-update-llmrouter`
- `llmrouter-update.service` -> `/etc/systemd/system/llmrouter-update.service`
- `llmrouter-update.timer` -> `/etc/systemd/system/llmrouter-update.timer`

The runtime configuration and secrets remain server-managed:

- `/opt/llmrouter/config.yaml`
- `/opt/llmrouter/.env`
- `/opt/llmrouter/data/`

`main` is published only after the Python/frontend tests and the read-only
container smoke pass. The timer polls that tag, reads its immutable Git commit
label, and delegates deployment to `update-llmrouter`. The update script pulls
the matching `sha-<40-character-commit>` image, pins its digest, creates an
online SQLite backup, verifies health/auth/dashboard/database integrity, and
rolls back on failure. A failed image ID is quarantined until `main` changes.

The Control Center is not exposed publicly. Reach it through an SSH tunnel to
the loopback-only port and open `/dashboard` locally.
