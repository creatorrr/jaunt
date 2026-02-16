# Claude Code Plugin & Skill Checklists

## Skill Creation Checklist

### 1. Define purpose and scope
- [ ] What problem does this skill solve?
- [ ] Who invokes it — the user, Claude, or both?
- [ ] Does it need arguments? What kind?
- [ ] Should it run in a fork (subagent) or inline?

### 2. Choose invocation settings
- [ ] `user-invocable: true` (user can type `/skill-name`) or `false` (background knowledge)
- [ ] `disable-model-invocation: true` (side effects like deploy/commit) or `false` (safe to auto-invoke)
- [ ] `argument-hint` set if the skill takes arguments

### 3. Write the SKILL.md
- [ ] YAML frontmatter with `name` and `description`
- [ ] Description includes keywords users would say naturally
- [ ] Markdown body under 500 lines
- [ ] Clear, actionable instructions (not vague guidelines)
- [ ] If using `context: fork`, the body includes a concrete task to perform

### 4. Add supporting files (optional)
- [ ] `references/*.md` for detailed documentation Claude can read on demand
- [ ] `scripts/*.sh` for automation (with `chmod +x`)
- [ ] `templates/*.md` for fill-in templates

### 5. Configure safety
- [ ] `allowed-tools` restricts tools to only what's needed
- [ ] No secrets or credentials in skill content
- [ ] Destructive skills use `disable-model-invocation: true`

### 6. Test
- [ ] Invoke with `/skill-name` and verify behavior
- [ ] Test with arguments if applicable
- [ ] Check `/context` to verify context budget is not exceeded
- [ ] Test auto-invocation (if enabled) by describing the task without using `/`

---

## Plugin Creation Checklist

### 1. Plan components
- [ ] List all skills the plugin provides
- [ ] List any hooks (PreToolUse, PostToolUse, etc.)
- [ ] List any MCP servers
- [ ] List any subagents

### 2. Create directory structure
```
my-plugin/
  .claude-plugin/
    plugin.json          # ONLY file here
  skills/
    skill-a/
      SKILL.md
    skill-b/
      SKILL.md
  hooks/
    hooks.json
  scripts/
    lint.sh
    format.sh
```

### 3. Write plugin.json
- [ ] `name` field set (becomes namespace prefix)
- [ ] `version` follows semver
- [ ] `description` is clear and concise
- [ ] `author` info included

### 4. Implement skills
- [ ] Each skill has its own directory under `skills/`
- [ ] Each `SKILL.md` has proper frontmatter
- [ ] Skills are self-contained (no cross-skill file references)

### 5. Implement hooks (if any)
- [ ] `hooks/hooks.json` has correct event names and matchers
- [ ] All referenced scripts exist and are executable
- [ ] Scripts output ONLY JSON to stdout (no shell noise)
- [ ] Timeouts are set for all hooks
- [ ] Paths use `${CLAUDE_PLUGIN_ROOT}` (not relative)

### 6. Verify structure
- [ ] Nothing except `plugin.json` is inside `.claude-plugin/`
- [ ] All paths in hooks use `${CLAUDE_PLUGIN_ROOT}`
- [ ] No `../` path references (plugins are cached elsewhere on install)

### 7. Test locally
- [ ] Run `claude --plugin-dir ./my-plugin`
- [ ] Verify all skills appear in `/` autocomplete
- [ ] Test each skill invocation
- [ ] Verify hooks fire on expected events
- [ ] Check `/context` for budget issues

### 8. Distribute
- [ ] Add to a marketplace or publish as a Git repo
- [ ] Document installation: `/plugin install name@marketplace`
- [ ] Include a README.md

---

## Hook Implementation Checklist

### 1. Choose the event
- [ ] Which lifecycle event should trigger this? (PreToolUse, PostToolUse, Stop, etc.)
- [ ] Does this hook need to block? (Only PreToolUse, UserPromptSubmit, PermissionRequest, Stop, SubagentStop can block)

### 2. Write the matcher
- [ ] Use specific tool names: `Write|Edit` (not `*`)
- [ ] Test the regex pattern against expected tool names

### 3. Implement the handler
- [ ] For `command` type: script outputs valid JSON to stdout
- [ ] Blocking hooks return `{"decision": "approve"}` or `{"decision": "block", "reason": "..."}`
- [ ] Non-blocking hooks: `decision` field is ignored but valid JSON still required
- [ ] Script is executable (`chmod +x`)
- [ ] Timeout is set (default is generous — set explicit limits)

### 4. Test
- [ ] Trigger the event manually and verify hook fires
- [ ] Test the block/approve logic
- [ ] Verify no shell profile noise in stdout
- [ ] Check `/hooks` to confirm hooks are loaded

---

## Marketplace Creation Checklist

### 1. Structure
```
my-marketplace/
  .claude-plugin/
    marketplace.json
  plugins/
    plugin-a/
      .claude-plugin/
        plugin.json
      skills/...
    plugin-b/
      ...
```

### 2. Write marketplace.json
- [ ] `name` set
- [ ] `owner` info included
- [ ] Each plugin has `name`, `source`, `description`, `version`

### 3. Test
- [ ] `/plugin marketplace add <path-or-repo>`
- [ ] `/plugin install <plugin>@<marketplace>`
- [ ] Verify installed plugins work

---

## Troubleshooting

### Skill not appearing in `/` menu
1. Check file is at `.claude/skills/<name>/SKILL.md` (not `.claude/skills/<name>.md`)
2. Verify YAML frontmatter is valid (no tabs, proper `---` delimiters)
3. Check `user-invocable` is not `false`
4. Run `/context` to check for budget issues

### Skill not auto-invoking
1. Check `disable-model-invocation` is not `true`
2. Improve the `description` — add keywords Claude would match on
3. Verify context budget is not exhausted (`/context`)

### Hook not firing
1. Hooks are snapshotted at session start — restart the session
2. Check `/hooks` to see loaded hooks
3. Verify the `matcher` regex matches the tool name exactly
4. Check script is executable

### Plugin skills not namespaced
1. Verify `.claude-plugin/plugin.json` has a `name` field
2. The namespace is the plugin name: `/plugin-name:skill-name`

### Context budget exceeded
1. Move rarely-used skills to `disable-model-invocation: true`
2. Shorten skill descriptions (max 1024 chars)
3. Move detailed content from `SKILL.md` to `references/` files
4. Override budget with `SLASH_COMMAND_TOOL_CHAR_BUDGET` env var
