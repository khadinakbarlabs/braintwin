# Braintwin (local)

One memory for all your AI tools, running on your own computer.

Braintwin gives Claude Code, Cursor, Codex, Claude Desktop and other tools a shared
memory. Tell one of them something once and the others know it too. This version runs
entirely on your machine: your memories sit in one private file, nothing is sent to a
server, and there is no charge.

## Install

You need Python 3.11 or newer.

```bash
curl -fsSL https://raw.githubusercontent.com/khadinakbarlabs/braintwin/main/install.sh | bash
```

Or download this repository and run `./install.sh` inside it.

The installer creates `~/.braintwin`, installs Braintwin there, and prints the exact
line to paste into each AI tool. Restart the tool afterwards.

## Connect a tool by hand

The installer prints these with the right path filled in.

**Claude Code**

```bash
claude mcp add braintwin -- ~/.braintwin/venv/bin/braintwin
```

**Cursor** (`~/.cursor/mcp.json`) and **Claude Desktop** (`claude_desktop_config.json`).
Use the full path the installer printed, not `~`:

```json
{ "mcpServers": { "braintwin": { "command": "/FULL/PATH/TO/.braintwin/venv/bin/braintwin" } } }
```

**Codex** (`~/.codex/config.toml`)

```toml
[mcp_servers.braintwin]
command = "/FULL/PATH/TO/.braintwin/venv/bin/braintwin"
```

## Where your memory lives

`~/.second-brain/brain.sqlite`, readable only by you. Set `SECOND_BRAIN_SQLITE` to use a
different file (use the same value in every tool).

## Useful commands

```bash
~/.braintwin/venv/bin/braintwin-cli health                 # is it working, how many memories
~/.braintwin/venv/bin/braintwin-cli export --out backup.json   # save everything to one file
```

## Move your memory to the cloud, or back

Braintwin also runs as a hosted service for web tools such as ChatGPT. You can copy your
memory in either direction. Copying twice never creates duplicates.

```bash
~/.braintwin/venv/bin/python -m pip install apify-client   # once
export APIFY_TOKEN=YOUR_APIFY_TOKEN

~/.braintwin/venv/bin/braintwin-cli move --from sqlite --to apify --dry-run   # preview
~/.braintwin/venv/bin/braintwin-cli move --from sqlite --to apify             # computer to cloud
~/.braintwin/venv/bin/braintwin-cli move --from apify --to sqlite             # cloud to computer
```

Keep your Apify token private. The cloud version has its own pricing; the local version
is free.

## Update or remove

- Update: run the install command again.
- Remove: delete `~/.braintwin`. Your memories stay in `~/.second-brain` until you delete
  that folder too.

## Licence

MIT. See `LICENSE`.
