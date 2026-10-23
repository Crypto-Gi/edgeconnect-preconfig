# Installing the skill in AI coding agents

Back to the [README](../README.md). `setup.sh` does this for you ([INSTALL.md](INSTALL.md)); this page is for installing the skill alone.

This skill follows the open [Agent Skills specification](https://agentskills.io) (`SKILL.md` + `scripts/` + `references/`).

```bash
# One-command install via npx skills (Claude Code, Codex, Cursor, Windsurf, OpenCode, etc.)
npx skills add Crypto-Gi/edgeconnect-preconfig
```

| Platform | Global Skill Path | Project-Level Skill Path | How to Invoke |
|---|---|---|---|
| **Claude Code (CLI)** | `~/.claude/skills/building-edgeconnect-preconfig-templates/` | `.claude/skills/building-edgeconnect-preconfig-templates/` | `/building-edgeconnect-preconfig-templates` or ask naturally in chat |
| **Claude Cowork / Desktop** | Upload `dist/building-edgeconnect-preconfig-templates.zip` (or `.skill`) in **Settings -> Capabilities -> Skills** | Attach skill folder to Cowork workspace | Automatically triggered when you ask to build or validate EdgeConnect preconfigs |
| **OpenAI Codex (CLI & App)** | `~/.codex/skills/building-edgeconnect-preconfig-templates/` or `~/.agents/skills/building-edgeconnect-preconfig-templates/` | `.agents/skills/building-edgeconnect-preconfig-templates/` | `$building-edgeconnect-preconfig-templates` or ask naturally |
| **Devin (CLI & IDE)** | `~/.config/devin/skills/building-edgeconnect-preconfig-templates/` | `.devin/skills/building-edgeconnect-preconfig-templates/` or `.agents/skills/building-edgeconnect-preconfig-templates/` | `/building-edgeconnect-preconfig-templates` or automatic skill matching |
| **Windsurf** | `~/.codeium/windsurf/skills/building-edgeconnect-preconfig-templates/` | `.windsurf/skills/building-edgeconnect-preconfig-templates/` or `.agents/skills/building-edgeconnect-preconfig-templates/` | `@building-edgeconnect-preconfig-templates` or automatic trigger in Cascade |
| **Google Antigravity** | `~/.gemini/antigravity/skills/building-edgeconnect-preconfig-templates/` | `.agent/skills/building-edgeconnect-preconfig-templates/` or `.agents/skills/building-edgeconnect-preconfig-templates/` | Automatically loaded from `.agent/skills/` or `.agents/skills/` |
| **OpenCode** | `~/.config/opencode/skills/building-edgeconnect-preconfig-templates/` | `.opencode/skills/building-edgeconnect-preconfig-templates/` or `.agents/skills/building-edgeconnect-preconfig-templates/` | Loaded automatically via OpenCode's native `skill` tool |
| **VS Code (Copilot Agent)** | `~/.claude/skills/building-edgeconnect-preconfig-templates/` | `.github/skills/building-edgeconnect-preconfig-templates/` | Enable `"chat.useAgentSkills": true` in VS Code Settings, then reference in Copilot Chat |

### Manual Copy Commands (Installs Only the Lean Skill Folder)

```bash
# Universal project-level install (Codex, Devin, Windsurf, Antigravity, OpenCode)
mkdir -p .agents/skills && cp -R skills/building-edgeconnect-preconfig-templates .agents/skills/

# Claude Code (global)
mkdir -p ~/.claude/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.claude/skills/

# OpenAI Codex (global)
mkdir -p ~/.codex/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.codex/skills/

# Devin CLI (global)
mkdir -p ~/.config/devin/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.config/devin/skills/

# Windsurf (global)
mkdir -p ~/.codeium/windsurf/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.codeium/windsurf/skills/

# Google Antigravity (global)
mkdir -p ~/.gemini/antigravity/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.gemini/antigravity/skills/

# OpenCode (global)
mkdir -p ~/.config/opencode/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.config/opencode/skills/

# VS Code GitHub Copilot (workspace)
mkdir -p .github/skills && cp -R skills/building-edgeconnect-preconfig-templates .github/skills/
```

## Conversational example (With Any Supported AI Agent)

1. **Capture & propose:**
   > *"Read appliance `branch-nyc-01` (or `2.NE`) from Orchestrator `prod` and build a preconfig Jinja2 and CSV template named `retail-branch`."*
2. **Review the variables:**
   The agent presents `variables.csv`: every field with the proposed decision (`variable` or `fixed`) and the reason; variables come first, then the rows marked `(check)` that need an owner decision. Reply in plain English:
   > *"Make the DNS servers per site, keep the region fixed, and use `core_ip` as the name of the LAN address."*
   The agent edits the sheet and re-runs `translate.py --variables variables.csv`. To replace the guesses with evidence, capture two or three more appliances of the same type and run `translate.py ... --compare <capture> ...`: fields that differ between the sites become variables and fields equal everywhere stay fixed.
3. **Validate rows & generate YAMLs:**
   Add rows for your new sites in `sites.csv` (or ask the agent to convert your IP plan spreadsheet into `sites.csv`), then say:
   > *"Validate `sites.csv` against `lab` and generate the YAML files."*
4. **Approve upload to Orchestrator:**
   After reviewing the validation and conflict table, say:
   > *"Approved — upload the ready preconfigs to `lab`."*
