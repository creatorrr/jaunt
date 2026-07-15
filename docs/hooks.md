Jaunt's [Codex plugin](/docs/guides/codex-plugin) and [Claude Code plugin](/docs/guides/claude-code-plugin) package generated-file guards and generated-`.pyi` protection. Review bundled hooks in the host before trusting them. The snippet below is the hand-written Claude equivalent.

# Warn-on-access hook

Add to `.claude/settings.json` in a Jaunt workspace:

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

`jaunt guard` returns an approval request with the owning spec when a file
lives under the configured generated directory. The Codex plugin needs an
adapter because Codex supplies a whole `apply_patch` command and does not
support `permissionDecision: "ask"`; its adapter checks every patch path and
returns `deny`.

Plugin hooks fail open when their payload, configuration, executable, or
timeout prevents a reliable decision. They are guardrails, not a complete
security boundary.

The first-party plugins also run a SessionStart freshness hook. It stays within
the nearest parent Jaunt workspace or the session directory's active Git
worktree, and it excludes nested Git repositories plus Claude/Codex managed
worktrees. Both lifecycle launchers return success even when Bash or another
optional tool is unavailable, so plugin setup problems do not fail the host
session.
