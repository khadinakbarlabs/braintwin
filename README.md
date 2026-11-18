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

## Save automatically

The installer prints the setup for Claude Code, Codex and Cursor. Add the matching
snippet to your existing settings and keep your other hooks. Replace
`/FULL/PATH/TO/.braintwin/venv/bin/braintwin-cli` with the path from the installer.
These hooks keep facts from your typed prompts in the local database. They use
built-in rules and read neither replies nor attachments. Session start gives the
selected AI tool a short set of relevant memories.

**Claude Code** (`~/.claude/settings.json`):

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "'/FULL/PATH/TO/.braintwin/venv/bin/braintwin-cli' hook session-start --client claude-code" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "'/FULL/PATH/TO/.braintwin/venv/bin/braintwin-cli' hook prompt --client claude-code" }] }]
  }
}
```

**Codex** (`~/.codex/hooks.json`):

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "'/FULL/PATH/TO/.braintwin/venv/bin/braintwin-cli' hook session-start --client codex" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "'/FULL/PATH/TO/.braintwin/venv/bin/braintwin-cli' hook prompt --client codex" }] }]
  }
}
```

After adding the commands, review and trust them with `/hooks` in Codex.

**Cursor** (`~/.cursor/hooks.json`):

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [{ "command": "'/FULL/PATH/TO/.braintwin/venv/bin/braintwin-cli' hook session-start --client cursor" }],
    "beforeSubmitPrompt": [{ "command": "'/FULL/PATH/TO/.braintwin/venv/bin/braintwin-cli' hook prompt --client cursor" }]
  }
}
```

To turn automatic saving off, remove the Braintwin prompt hook from that tool's
settings. Remove the session-start hook too if you want to stop loading memories.

## Where your memory lives

`~/.local/share/braintwin/brain.sqlite`, readable only by you. Set `BRAINTWIN_SQLITE` to use a
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
- Remove: delete `~/.braintwin`. Your memories stay in `~/.local/share/braintwin` until you delete
  that folder too.

## Licence

MIT. See `LICENSE`.

Existing installations keep using their existing database. Earlier configuration names remain supported. An explicit `BRAINTWIN_SQLITE` path takes precedence.

For a question about earlier work, call `memory_context` with `compact=true` and the project scope. To verify a detail, call `memory_search` with a quoted phrase or the returned memory ID and `compact=true`. Use the returned continuation to read more.
