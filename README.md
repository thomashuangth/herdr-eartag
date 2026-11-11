# Eartag

An ear tag for every agent in your herd: a panel on the right of each agent pane
in [herdr](https://herdr.dev). When you come back to an agent, one glance tells
you what the conversation is about, what you asked, what the agent did and
whether it is done, plus context and quota left.

![Eartag next to a Claude Code agent in herdr](docs/hero.png)

- **Header**: project, permission mode, agent state in herdr's own state colors.
- **Now / last ask**: the conversation's subject, your latest request (a title plus
  key points when rephrasing is on), the tool running right now, task progress, and
  what the agent did, under a colored verdict: `✓ DONE`, `◐ PARTIAL`, `✗ FAILED`,
  `? YOUR TURN` (with the agent's question) or `◐ WORKING`. Key words (the object,
  files, numbers, results) are highlighted.
- **Before**: your three previous asks, each with its outcome (✓ ◐ ✗ ?) when
  rephrasing is on.
- **Files** edited in this session, with the repository's uncommitted
  `+added −removed` lines; how long the ask took; alerts (other agents on the same
  branch, detached HEAD).
- **Footer**: context used, 5-hour and 7-day quotas (click or hover a bar for the
  reset time, shown in place without moving anything), machine, branch, model and
  effort.
- **Model and effort** on each family's own scale (Claude 5 levels, OpenAI 5,
  Gemini and Grok 2), colored cool for low, gold for high, red for max.
- **Claude Code, Codex and Antigravity** (`agy`) all get the full panel, including
  several Claude Code accounts side by side (`CLAUDE_CONFIG_DIR=~/.claude-work claude`),
  each with its own quotas.

![Done, waiting for you, failed](docs/states.png)

<img src="docs/hover.png" alt="Hovering a gauge shows its reset time" width="360" align="right">

The panel follows your herdr theme (`[theme]` in `config.toml`, including
`[theme.custom]`), speaks English and French ([add yours](#translations)), and has
an ASCII mode. Hover or click a gauge to see when it resets.

<br clear="right">

*Screenshots use made-up projects.*

## Requirements

herdr 0.9.3+ on macOS or Linux, and Python 3.9+ (already there on macOS and
most Linux distributions; nothing to `pip install`). The full panel is for Claude
Code, Codex and Antigravity; other agents get their state, branch and the files
changed in git.

## Install

```sh
herdr plugin install thomashuangth/herdr-eartag
```

That's it: a panel now opens on the right of every new agent.

The first time, click **▸ Enable quotas + prefix+i** at the bottom of a panel.
This one click:

- puts a small relay in front of your Claude Code statusline (in `~/.claude` and
  every other Claude config dir, such as `~/.claude-work`) and your Antigravity
  statusline, so the panel gets the 5h/7d quotas and the exact context (your
  statusline keeps running and printing as before; each `settings.json` is backed
  up first). Codex needs nothing: its quotas are in its session files;
- runs `herdr integration install claude` for every Claude config dir that lacks
  herdr's session hook, so herdr reports the exact session of each account (herdr
  installs it only for the account of the shell it runs in);
- binds `prefix+i` to show or hide the panel, if that key is free (your herdr
  `config.toml` is backed up first, and herdr reloads it).

Prefer the terminal? `herdr plugin action invoke thomashuangth.eartag.setup` does
the same. Undo it all with `herdr plugin action invoke thomashuangth.eartag.undo-setup`
(it removes only the session hooks Eartag added).

For agents that were already running before the install, focus one and press
`prefix+i`.

### Update and uninstall

Open panels pick up an update by themselves: each one re-executes in place a
couple of seconds after the plugin's files change (also when you edit a copy
linked with `herdr plugin link`). An update that fails to load is ignored and the
panel keeps running the previous code.

```sh
herdr plugin update thomashuangth.eartag     # herdr 0.9.5+ (on 0.9.3: run the install command again)

herdr plugin action invoke thomashuangth.eartag.undo-setup
herdr plugin uninstall thomashuangth.eartag
```

## Showing and hiding

- The panel opens automatically when herdr detects an agent in a pane.
- `prefix+i` (if bound) toggles it for the focused agent, from the agent pane or
  from the panel itself.
- The `⋯` button at the bottom right opens the menu (arrows and Enter work while
  it is open):
  hide for this agent, hide for this workspace, stop opening automatically, turn
  rephrasing on or off.
- Hidden agents stay hidden until you press `prefix+i` on them again, or run
  `herdr plugin action invoke thomashuangth.eartag.unhide` to show them all.

## Configuration

`$(herdr plugin config-dir thomashuangth.eartag)/config.ini`, created on first run
with comments. Changes apply to panels opened afterwards.

| Key | Default | |
|---|---|---|
| `language` | `auto` | `auto` (system language when translated, else English), `en`, `fr` |
| `auto_open` | `true` | open next to every new agent |
| `width` | `40` | panel width in columns |
| `icons` | `unicode` | `ascii` for fonts without geometric symbols |
| `colors` | `herdr` | `herdr` follows the herdr theme, `ansi` uses the terminal's 16 colors |
| `background` | `theme` | `theme` paints the panel with the theme's surface color, `terminal` keeps your terminal background |
| `host_label` | | footer machine name (default: `Mac` on macOS, short hostname elsewhere) |
| `summarize` | `off` | `auto`, `claude`, `codex` or `gemini`: see below |
| `summarize_model` | | empty = `haiku` for claude, the CLI's default for codex and gemini |
| `claude_dirs` | | extra Claude Code config dirs, comma separated (`~/.claude` and `~/.claude-*` are found without it) |

### Several Claude Code accounts

The panel runs in herdr's environment, not your agent's, so it cannot see the
`CLAUDE_CONFIG_DIR` of an alias like `claude-work`. It looks in `~/.claude`, every
`~/.claude-*` or `~/.claude_*` dir holding transcripts, and `claude_dirs`. For
herdr to tell each agent's session apart, install its Claude integration for that
dir too: `CLAUDE_CONFIG_DIR=~/.claude-work herdr integration install claude`.
Without it, Eartag matches the session by the pane's title.

## Rephrasing with an LLM

Off by default. Turn it on from the panel menu (`⋯` → *Rephrase asks*)
or with `summarize` in the config file. When on, each ask is read in the context
of the conversation: the panel shows the overall work going on (it replaces
Claude Code's session title, which is set early and goes stale), what your latest
message does within it (*new*, *follow-up*, *fix*, *approval* or *question*) as a
short imperative with its key points, and the agent's latest message as a colored
verdict plus what changed, with the key words highlighted. A plain "yes, go" reads
as what it approves.

When it is off, a short ask that answers a question from the agent shows that
question under it (`↳ answering: Shall I also patch the tests?`).

**How it connects.** Eartag has no API key and no server. It runs a coding-agent
CLI you already have and are logged in to, in its non-interactive mode, under
your own account:

| `summarize` | Command | Notes |
|---|---|---|
| `claude` | `claude -p --no-session-persistence --tools ""` | model `haiku` by default; no tools, MCP servers, skills or hooks |
| `codex` | `codex exec --ephemeral --sandbox read-only` | |
| `gemini` | `gemini --approval-mode plan` | |
| `auto` | first found among `claude`, `codex`, `gemini` | |

Calls run from an empty private folder, never persist a session, and cannot run
commands or edit files, so text in a transcript cannot make them act. If the CLI
is missing or fails, the panel shows your raw ask. Turning rephrasing on from the
menu shows a notice saying what is sent and to whom.

**What is sent, and when.**

- For each new ask: its text, the end of the agent's message just before it (up to
  1,500 characters), your five previous asks (300 characters each at most) and the
  overall goal found so far.
- The agent's latest message each time it changes. Results are cached on disk,
  mode `0600`, so each ask is sent once.
- Nothing else, and nothing at all while rephrasing is off.

**Cost and policies.** Each call counts against your plan's usage (it shows up in
your 5h quota) or your API billing. Your provider's data retention and privacy
policies apply. Do not turn this on for code you are not allowed to send to that
provider.

## Privacy

With `summarize = off`, nothing leaves your machine: no network calls, no telemetry.

- The panel reads Claude Code, Codex and Antigravity transcripts locally, tail only, and shows file
  **basenames** and program names (never full paths, command arguments or tool
  inputs).
- The statusline relay stores a whitelist: session id, model, effort, context
  window and rate limits. It drops `cwd`, transcript path, cost and anything else.
  Files are written with mode `0600` under `~/.local/state/herdr-eartag/`.
- Rephrasing is the only feature that sends data out: see the section above.

## Troubleshooting

```sh
herdr plugin action invoke thomashuangth.eartag.doctor   # paths, agents, relay, rephrasing backend
herdr plugin log list --plugin thomashuangth.eartag      # read its report and other logs
```

From the plugin directory (`herdr plugin list` shows it), `python3 eartag.py preview w1:p1`
renders the panel of a pane id in your terminal.

- Quota bars stay empty: click **▸ Enable quotas + prefix+i** (or run the `setup` action), then wait for Claude's next reply.
- `prefix+i` does nothing: it was already used in your herdr config, so Eartag left it alone; bind `thomashuangth.eartag.toggle` to another key.
- Rephrased asks never appear: run the doctor action; it names the backend it found.
- *“session inferred from folder”*: herdr did not report the Claude session id,
  so the panel follows the newest transcript of that folder. When that account
  lacks herdr's session hook, the panel shows **▸ Fix** under the note: click it
  (or run the `setup` action). Sessions started afterwards are exact.
- The panel opens at 30 columns instead of 40 when the agent would keep fewer
  than 60 columns.
- Clicking the panel hands the keyboard back to the agent right away, and text
  typed while the panel has focus is passed on to the agent.
- herdr only resizes terminals that are on screen: a panel opened in a
  background workspace adopts its final width when you switch to it.

## Translations

All interface text lives in `herdr_eartag/i18n.py`, one table per language
(`en`, `fr`). To add one, copy the `en` table under your language code (`de`,
`es`, `ja`…), translate the values and open a pull request. The system language
is then picked up automatically, and any missing key falls back to English.

Keep labels short: the panel is 40 columns wide, and wide characters (Chinese,
Japanese, Korean) take two columns each. `fmt.*` keys hold date and duration
formats, `days` and `months` the short names.

## Development

```sh
herdr plugin link .                    # use your working tree
python3 -m unittest discover -s tests  # stdlib only, Python 3.9+
```

Layout lives in `herdr_eartag/render.py` (pure, unit tested), data collection in
`panel.py`, terminal I/O in `tui.py`.

## License

MIT
