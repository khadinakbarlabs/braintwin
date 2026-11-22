# Braintwin

Give your AI tools one place to look up project decisions, preferences and notes. Save “Keep release reports to three bullets” in one connected tool, then ask another for the rule and its memory ID.

**Using Claude (web, desktop or Claude Code)?** Install the Braintwin plugin. In Claude, open Plugins → Add → Add marketplace, enter `khadinakbarlabs/braintwin` and install Braintwin. In Claude Code, run `/plugin install braintwin --marketplace khadinakbarlabs/braintwin`. Sign in with Apify when asked. The plugin adds the memory tools and tells Claude to save lasting preferences in Braintwin.

**Want your agent to save without being asked?** Choose the automatic setup for [Claude Code](https://braintwin.dev/guides/claude-code/), [Codex](https://braintwin.dev/guides/codex/), [Cursor](https://braintwin.dev/guides/cursor/) or [Gemini CLI](https://braintwin.dev/guides/gemini-cli/). One setup adds both the memory tools and the prompt handler. The standard connector in web chats leaves tool use to the app.

## Choose where your memory lives

| Where you want to use Braintwin | Start here |
| --- | --- |
| Across web apps, editors and agents, with memories stored in your Apify account | [Sign in with Apify](https://braintwin.dev/app/?signin=1#connect) or [choose your AI tool](https://braintwin.dev/guides/) |
| On your own machine, with a local SQLite file and no Apify account | [Choose automatic saving](#save-automatically) or [connect a tool by hand](#connect-a-tool-by-hand) |

**Cloud setup guides**

| Chat apps | Editors and terminals | Agent workspaces |
| --- | --- | --- |
| [ChatGPT](https://braintwin.dev/guides/chatgpt/) | [Cursor](https://braintwin.dev/guides/cursor/) · [VS Code](https://braintwin.dev/guides/vscode/) | [Hermes Agent](https://braintwin.dev/guides/hermes/) |
| [Claude](https://braintwin.dev/guides/claude/) | [Codex](https://braintwin.dev/guides/codex/) · [Claude Code](https://braintwin.dev/guides/claude-code/) | [OpenClaw](https://braintwin.dev/guides/openclaw/) |
| [Grok](https://braintwin.dev/guides/grok/) | [OpenCode](https://braintwin.dev/guides/opencode/) · [Grok Build](https://braintwin.dev/guides/grok-build/) · [Gemini CLI](https://braintwin.dev/guides/gemini-cli/) | [Muse Code](https://braintwin.dev/guides/muse/) |

Cursor and VS Code have install buttons. Other guides give the connector address, terminal command or settings for that app. Authorize each cloud client with the same Apify account. In a new conversation, ask **“Which Braintwin memory tools can you use?”** to check discovery before saving a note. The local package and cloud server keep separate stores until you [move or copy your memories](#move-your-memory-to-the-cloud-or-back).

## Use the cloud dashboard

| What you want to do | Where to go |
| --- | --- |
| Follow a YouTube channel, news site, blog, newsletter or feed | Choose a source type, paste its address and preview the review policy and check frequency. [Sources](https://braintwin.dev/app/#follow) |
| See what was saved and why | Open a project in the map, then choose notes, agent work or a source to see the underlying memories. [Memory map](https://braintwin.dev/app/#library) |
| Check an agent setup | Pick your agent and follow its verification prompt. Active records the last successful setup check. Remove the connector inside the client to disconnect it. [Connections](https://braintwin.dev/app/#connect) |
| Use your own AI account | Choose OpenAI, Anthropic, Google Gemini or Mistral, a model and a purpose. Drafting sends the selected passage to the provider and lets you edit the result before saving. The draft key lasts until the tab reloads or you sign out. [AI providers](https://braintwin.dev/app/#providers) |

Cloud sign-in uses Apify. The current hosted Actor also exposes a separate URL for each running container. Anyone who obtains that URL can reach its memory service while the run is active. Keep it private and use the local package for sensitive memories.

## Install

Use Python 3.11 or newer. Clone the repository and run the installer from its folder:

```bash
git clone https://github.com/khadinakbarlabs/braintwin.git
cd braintwin
bash install.sh
```

The installer sets up Braintwin in `~/.braintwin`, connects every supported coding tool it finds (Claude Code, Codex, Cursor, Gemini CLI) and turns on automatic saving to your Apify account. Name one tool, such as `bash install.sh cursor`, to connect only that tool. Add `--storage local` to keep memories on this computer with no Apify account. Once connected, [check your setup](#check-your-setup) or [enable automatic saving](#save-automatically).

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

In **Claude Code**, **Codex**, **Cursor** and **Gemini CLI**, one command adds the connector and registers it with the app’s prompt events. Pick your app and where to save. On macOS or Linux with Git and Python 3.11+, run the command from its [setup guide](https://braintwin.dev/guides/), or from this repository:

```bash
bash install.sh cursor --storage apify
```

Replace `cursor` with `claude-code`, `codex` or `gemini-cli`, or leave the name out to connect every tool found. Memories go to the Apify account you use for the dashboard (the default). Restart the app and allow its Braintwin hooks. In Codex, review them in `/hooks`. Some managed workspaces require an administrator to enable hooks. If you added Braintwin's web address to the app before, setup replaces that entry for you.

To keep memories on this computer instead:

```bash
bash install.sh cursor --storage local
```

Apify opens its consent page once. The installed connector and its prompt handler share that account. Apify requests full API access. Braintwin keeps the authorization in macOS Keychain or Linux Secret Service. Hosted saves use the Actor’s published rates. The current Actor’s separate container URL can expose its memory service while a run is active. Choose local storage for sensitive work.

Try it with an ordinary message. **“I prefer weekly status reports on Fridays.”** In a fresh conversation in the same project, ask **“When do I prefer weekly reports?”** An Apify save also appears in your dashboard’s Memories or Waiting for review. Local saves stay on the computer. Your other local agents can recall them when they use the same file.

Braintwin checks the submitted text for lasting statements, skips recognized credentials and explicit requests such as “don’t save this,” and respects the owner’s review policy. It reads the typed prompt field. Hosted automatic capture sends the selected sentences with the agent and project labels. A connection failure can leave a save unconfirmed. Check the receipt and search your memories before trying again.

```bash
~/.braintwin/venv/bin/braintwin-cli connection-status cursor
~/.braintwin/venv/bin/braintwin-cli disconnect cursor
```

Disconnecting stops the installed callbacks for that app. You can run the installer again later. To change storage, disconnect first and rerun your chosen setup. To clear the installed connector’s local Apify authorization, run `braintwin-cli connector-logout` from the same directory. You can also revoke access in Apify.

The connector address `https://braintwin.dev/mcp/` gives ChatGPT, Claude web, Grok and other connected apps the memory tools. These apps cannot run automatic hooks, so each one chooses when to call the tools and when to ask for approval. If the app has its own memory, it may save there instead. An explicit “Save this in Braintwin” is the most reliable way to request a save in those apps.

## Check your setup

1. In your first agent, say “Use Braintwin to remember that my weekly report starts with a revenue summary.”
2. In a new chat or a second agent on the same machine, say “Use Braintwin to find my weekly report preference.”
3. If recall is empty, confirm Braintwin appears in both agents’ MCP lists and both use the same local memory file.

Try the terminal health check to confirm the local store opens:

```bash
~/.braintwin/venv/bin/braintwin-cli health
```

For the automatic setup, use the ordinary preference example above. This explicit save-and-search check also works in tool-only clients.

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
- Before removal, run `braintwin-cli disconnect` for each installed agent and `braintwin-cli connector-logout` if you connected Apify. Then delete `~/.braintwin`. Your memories stay in `~/.local/share/braintwin` until you delete
  that folder too.

## Licence

MIT. See `LICENSE`.

Existing installations keep using their existing database. Earlier configuration names remain supported. An explicit `BRAINTWIN_SQLITE` path takes precedence.

For a question about earlier work, call `memory_context` with `compact=true` and the project scope. To verify a detail, call `memory_search` with a quoted phrase or the returned memory ID and `compact=true`. Use the returned continuation to read more.
