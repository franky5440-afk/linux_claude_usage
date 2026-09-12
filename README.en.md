# Claude Usage Widget

[繁體中文](README.md)

A Cinnamon desklet that keeps your [Claude Code](https://claude.com/claude-code) usage on your Linux desktop: quota bars, cost estimates, a per-project token breakdown, how much context each live conversation is using, and a clickable weekly history report. No browser tab, no CLI command — it's just there.

![license](https://img.shields.io/badge/license-Apache%202.0-blue)

## Features

- **Quota usage**: 5-hour session limit, weekly limit, and per-model weekly limits, each with a progress bar and a "resets in X hours" countdown
- **Cost estimate**: today's and this week's spend in USD, broken down by model (estimate only — actual billing follows your Claude subscription plan, not this number)
- **Project ranking**: today's top 5 projects by token usage
- **Session context**: how much context each in-progress conversation is currently holding, and what share of the context window that is (e.g. `13.5% of 1M`). Shows conversations active in the last 5 minutes, up to 3 of them
- **Weekly history report**: click the desklet to open a locally generated HTML report, tokens and cost rolled up by week

## Requirements

- Linux desktop with **Cinnamon 6.6+**
- **Python 3.9+** (tested on 3.12; runtime uses only the standard library, no third-party packages)
- A logged-in [Claude Code CLI](https://claude.com/claude-code) (reads the OAuth token from `~/.claude/.credentials.json` to check your quota)

## Installation

⚠️ The desklet hardcodes the collector's path as `~/Claude/linux_claude_usage`, so this repo **must be cloned to exactly that path** to work.

```bash
mkdir -p ~/Claude
git clone https://github.com/franky5440-afk/linux_claude_usage.git ~/Claude/linux_claude_usage
cd ~/Claude/linux_claude_usage

# Create the venv (no packages needed at runtime — this just matches the
# path the desklet expects)
python3 -m venv .venv

# Only needed if you want to run the test suite
.venv/bin/pip install pytest

# Copy the desklet files into Cinnamon's desklets directory
./install.sh
```

After installing:

1. Open Cinnamon Settings → Desklets
2. Find "Claude 用量監控" ("Claude Usage Monitor") under available desklets and add it to your desktop
3. Give the collector something to show — run it once by hand, or schedule it with cron:

   ```bash
   cd ~/Claude/linux_claude_usage && .venv/bin/python -m collector.main
   ```

   Example crontab entry (runs every minute):

   ```
   * * * * * cd ~/Claude/linux_claude_usage && .venv/bin/python -m collector.main
   ```

4. Right-click the desklet on your desktop → Configure, to adjust the update interval, toggle the cost/project/session sections, or change the width

## Architecture

Two layers connected by a single JSON file:

- **collector** (Python): periodically calls the Usage API for quota percentages, incrementally scans `~/.claude/projects/**/*.jsonl` transcripts to compute token counts and cost, and writes `~/.cache/claude-usage-widget/state.json`
- **desklet** (GJS / Cinnamon): reads `state.json` and renders the UI — it never touches credentials or transcripts directly

Full spec in [SPEC.md](SPEC.md).

### Quota percentage vs. session context percentage

These two numbers look alike but mean **entirely different things**:

| | Quota usage | Session context |
|---|---|---|
| What it measures | How much of your allowance the billing window has consumed | How much of the context window this conversation currently holds |
| When it drops | Automatically, when the window resets | After the conversation is compacted |
| Where it comes from | Usage API | The `usage` field of the last assistant message in the transcript |

Context usage is `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`
from that last message. The denominator comes from the model id recorded in the transcript
(`[1m]` means 1,000,000, otherwise 200,000). **When it can't be determined, only the token
count is shown and the percentage is left blank** — no guessed denominator.

## Security

- **Read-only credentials**: only reads the `accessToken` from `~/.claude/.credentials.json`, never attempts to refresh or overwrite it
- **The token never leaves memory**: it's never written to logs, `state.json`, error messages, or any file
- **Talks to exactly one host**: `api.anthropic.com` — no telemetry, no other outbound connections
- `state.json` is written with `0600` permissions

## Disclaimer

The cost figures shown are **estimates only**, not an official bill. Actual billing follows your Claude account's subscription plan.

## License

[Apache License 2.0](LICENSE)
