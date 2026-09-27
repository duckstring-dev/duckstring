---
title: Running on a Server
description: Set up an always-on, shared Catchment.
---

# Running on a Server

A Catchment on your laptop stops when you close it. For pipelines that need to keep running, and for a team to share, run the same Catchment on a machine that stays on. Nothing about the Ponds changes. This guide sets one up on a Linux server under systemd; the same steps apply to any machine or container. For a hosting platform that runs Python web apps, see [Hosting on a Platform](hosting_on_a_platform.md).

## Install

Create a user for the Catchment and install Duckstring into a virtual environment, with any packages your Ripples import:

```bash
sudo useradd --system --create-home duckstring
sudo -iu duckstring
python3 -m venv ~/venv
~/venv/bin/pip install duckstring
~/venv/bin/pip install 'duckstring[dbt]' scikit-learn   # whatever your Ponds need
```

Ripples run in this environment, so install new dependencies here before deploying Ponds that use them.

## Create the Catchment

Still as the `duckstring` user, create and register the Catchment without starting it:

```bash
~/venv/bin/duckstring catchment init --name prod --host 127.0.0.1 --port 7474 --generate-key --no-start
```

`--generate-key` creates three API keys and prints them once. Store them in your password manager now:

| Key | Give it to |
|---|---|
| read | analysts, dashboards and BI tools that only read data |
| demand | applications that also trigger refreshes |
| full | people and CI jobs that deploy and operate Ponds |

The full key is stored in the Catchment's registration in `~/.duckstring/config.toml`, so the CLI on this machine keeps working. State goes to `~/.duckstring/prod` unless you pass `--root`.

Binding to `127.0.0.1` keeps the Catchment private to the machine, with a reverse proxy providing TLS in front of it (below). Bind to `0.0.0.0` only on a private network you trust.

## Run it under systemd

```ini
# /etc/systemd/system/duckstring.service
[Unit]
Description=Duckstring Catchment
After=network-online.target

[Service]
User=duckstring
ExecStart=/home/duckstring/venv/bin/duckstring catchment start prod
Restart=on-failure
Environment=DUCKSTRING_MEMORY_LIMIT=12GB

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now duckstring
```

Run exactly one Catchment process per state directory. Set `DUCKSTRING_MEMORY_LIMIT` to about 80% of the memory you want Ducks to use, and any other [environment variables](../reference/environment.md) the same way.

## Put TLS in front

API keys and secrets travel in requests, so serve the Catchment over HTTPS. With Caddy, which obtains certificates automatically:

```text
# /etc/caddy/Caddyfile
catchment.example.com {
    reverse_proxy 127.0.0.1:7474
}
```

The web UI is served at the same address.

## Connect from your machine

Register the server with the CLI on each workstation:

```bash
duckstring catchment connect --name prod --path https://catchment.example.com --key "$FULL_KEY" --yes
duckstring status
```

From then on, commands use `prod` by default, or with `-c prod`. Deploy from a Pond's directory as usual:

```bash
duckstring pond deploy
```

For deploys from CI, give the pipeline the full key as a secret and connect it the same way.

## Where the data lives

By default, published tables are stored under the state directory on the server's disk. To keep them in object storage instead, set a data root when creating the Catchment, before any Pond publishes:

```bash
duckstring catchment init --name prod --data-root 's3://acme-lake/duckstring?region=eu-west-2' ...
```

An object-store data root is also what enables [cloud compute](cloud_compute_on_aws.md). One Catchment should own a data root; give each Catchment its own prefix.

When disk space runs low, the Catchment frees space by removing the working databases of idle Ponds, which are rebuilt when next needed. The threshold is `DUCKSTRING_MIN_FREE_BYTES`, 1 GiB by default.

## Backups

The state directory holds deployed code, run history and configuration. Back it up with a filesystem snapshot, or download it over the API while no runs are in progress:

```bash
duckstring catchment download -c prod --path backups/prod-$(date +%F)
```

On disks that don't persist, such as a container without a volume, have the Catchment copy its state to object storage continuously instead:

```bash
duckstring catchment init --name prod --state-backup 's3://acme-lake/duckstring-state' --checkpoint-every 60s ...
```

A Catchment starting with an empty state directory restores from its state backup automatically.

## Maintenance

**Rotating keys.** Replace keys without restarting, for example when someone leaves:

```bash
duckstring catchment rotate-keys -c prod --level read
```

Running Ducks are unaffected. Hand out the new key; the old one stops working immediately.

**Upgrading Duckstring.** Install the new version and restart the service. Runs interrupted by the restart resume when the Catchment starts again, re-running only the Ripples that hadn't finished.

```bash
sudo -u duckstring /home/duckstring/venv/bin/pip install -U duckstring
sudo systemctl restart duckstring
```
