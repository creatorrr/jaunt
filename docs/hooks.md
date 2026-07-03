# Warn-on-access hook

Add to `.claude/settings.json` in a jaunt project:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|MultiEdit|Write|Read|NotebookEdit",
        "hooks": [{"type": "command", "command": "jaunt guard"}]
      }
    ]
  }
}
```

Agents get a confirmation prompt with a pointer to the owning spec when they touch
`__generated__/**`. For harnesses without hook support (Codex), the barrier is advisory:
`jaunt instructions` states the rule.
