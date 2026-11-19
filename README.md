# Braintwin

Give your AI tools one place to look up project decisions, preferences and notes. Save “Keep release reports to three bullets” in one connected tool, then ask another for the rule and its memory ID.

## Choose where your memory lives

| Where you want to use Braintwin | Start here |
| --- | --- |
| Across web apps, editors and agents, with memories stored in your Apify account | [Sign in with Apify](https://braintwin.dev/app/?signin=1#connect) or [choose your AI tool](https://braintwin.dev/guides/) |
| On your own machine, with a local SQLite file and no Apify account | [Install the local package](#install), then [connect a tool by hand](#connect-a-tool-by-hand) |

**Cloud setup guides**

| Chat apps | Editors and terminals | Agent workspaces |
| --- | --- | --- |
| [ChatGPT](https://braintwin.dev/guides/chatgpt/) | [Cursor](https://braintwin.dev/guides/cursor/) · [VS Code](https://braintwin.dev/guides/vscode/) | [Hermes Agent](https://braintwin.dev/guides/hermes/) |
| [Claude](https://braintwin.dev/guides/claude/) | [Codex](https://braintwin.dev/guides/codex/) · [Claude Code](https://braintwin.dev/guides/claude-code/) | [OpenClaw](https://braintwin.dev/guides/openclaw/) |
| [Grok](https://braintwin.dev/guides/grok/) | [OpenCode](https://braintwin.dev/guides/opencode/) · [Grok Build](https://braintwin.dev/guides/grok-build/) · [Gemini CLI](https://braintwin.dev/guides/gemini-cli/) | [Muse Code](https://braintwin.dev/guides/muse/) |

Cursor and VS Code have install buttons. Other guides give the connector address, terminal command or settings for that app. Authorize each cloud client with the same Apify account. In a new conversation, ask **“Which Braintwin memory tools can you use?”** to check discovery before saving a note. The local package and cloud server keep separate stores until you [move or copy your memories](#move-your-memory-to-the-cloud-or-back).

## Install

Use Python 3.11 or newer. Clone the repository and run the installer from its folder:

```bash
git clone https://github.com/khadinakbarlabs/braintwin.git
cd braintwin
bash install.sh
```

The installer sets up Braintwin in `~/.braintwin` and prints the full path to the MCP executable. A local setup needs no Apify account. Once connected, [check your setup](#check-your-setup) or [enable automatic saving](#save-automatically).

## Connect a tool by hand

Copy the full executable path printed by the installer. Replace `/FULL/PATH/TO/braintwin` with it in the examples below. If you already have an MCP configuration, merge in the `braintwin` entry and keep the others. Restart the client after saving.

### Cursor and Claude Desktop

In Cursor, open your personal `~/.cursor/mcp.json`. In Claude Desktop, use **Settings → Developer → Edit Config**. Merge this JSON into the existing file.

```json
{ "mcpServers": { "braintwin": { "command": "/FULL/PATH/TO/braintwin" } } }
```

### Codex and Grok Build

Use `~/.codex/config.toml` for Codex or `~/.grok/config.toml` for Grok Build. Check `/mcp` in Codex or run `grok mcp doctor braintwin`.

```toml
[mcp_servers.braintwin]
command = "/FULL/PATH/TO/braintwin"
```

### Claude Code

Run this in a terminal. Check `/mcp` in Claude Code afterwards.

```bash
claude mcp add --scope user braintwin -- "/FULL/PATH/TO/braintwin"
```

### Hermes Agent

Open the `config.yaml` for your Hermes profile and add the entry under `mcp_servers`.

```yaml
mcp_servers:
  braintwin:
    command: "/FULL/PATH/TO/braintwin"
    args: []
```

### OpenClaw

Use an OpenClaw Gateway release that supports outbound MCP servers and the reload and probe commands. Run these on the Gateway machine. The executable path must exist on that machine.

```bash
openclaw mcp set braintwin '{"command":"/FULL/PATH/TO/braintwin","args":[]}'
openclaw mcp reload
openclaw mcp probe braintwin
```

### OpenCode

Merge this entry into your OpenCode configuration and restart the session. Run `opencode mcp list` to check the server status.

```json
{ "mcp": { "braintwin": { "type": "local", "command": ["/FULL/PATH/TO/braintwin"], "enabled": true } } }
```

### Gemini CLI

Run this in your terminal, using the executable path from the installer. Enter `/mcp list` in a new Gemini CLI session to check the server.

```bash
gemini mcp add --scope user braintwin "/FULL/PATH/TO/braintwin"
```

### Muse Code

Add the server to `~/.config/muse/settings.json`. Open Muse Code and run `/mcp`.

```json
{
  "mcp_servers": {
    "braintwin": { "transport": "stdio", "command": "/FULL/PATH/TO/braintwin", "args": [], "mode": "optional" }
  }
}
```

### VS Code

Use **MCP: Open User Configuration** from the Command Palette and add this server. Start it with **MCP: List Servers** and try a request in Agent mode.

```json
{ "servers": { "braintwin": { "type": "stdio", "command": "/FULL/PATH/TO/braintwin" } } }
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

## Check your setup

1. In your first agent, say “Use Braintwin to remember that my weekly report starts with a revenue summary.”
2. In a new chat or a second agent on the same machine, say “Use Braintwin to find my weekly report preference.”
3. If recall is empty, confirm Braintwin appears in both agents’ MCP lists and both use the same local memory file.

Try the terminal health check to confirm the local store opens:

```bash
~/.braintwin/venv/bin/braintwin-cli health
```

Automatic saving is optional and has separate setup for Claude Code, Codex, and Cursor. The other guides connect the four memory tools. You can ask those agents to save and recall directly.

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
