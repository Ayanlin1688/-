# Yanlin Smart-Creation Matrix project workflow

The user authorizes incremental work on `main` in this project and automatic pushes to both repositories after each completed, verified logical code change. Do not ask again for permission to perform those commits/pushes.

- `origin` stays `https://github.com/admin11044/StoryboardVideoStudio.git`. The second remote `ayanlin` is `https://github.com/Ayanlin1688/-.git`. Add `ayanlin` when it is missing; stop if its URL differs. Never replace `origin` and never force-push.
- Preserve existing work and run relevant tests before committing. Use Chinese commit descriptions with `feat:`, `fix:`, `refactor:` or `docs:`.
- Run `python -X utf8 scripts/sync_github.py --message-file <UTF-8 message file>` after verification. It commits once, pushes `origin` and then `ayanlin`, verifies each `refs/heads/main`, and retries only a failed side up to 3 attempts without creating another commit.
- Confirm both remotes' `main` equal local HEAD. After each successful push tell the user: `已同步到GitHub，最新commit: <hash>`, including the full hash for both repositories.
- Never commit config.json, API keys, tokens, environment files, generated videos, execution logs, screenshots, or local real-material diagnostic artifacts. Keep .gitignore protections and secret checks intact. Credentials belong in the local config or Git Credential Manager, never in code/remote URLs/chat.
- If authentication/network blocks a push, retain the local commits, complete independent authorized work, and report the actual blocker. Do not claim remote sync succeeded.
- Keep the existing PyQt5 + QFluentWidgets stack; do not mix Qt bindings. Tests use isolated configuration and local HTTP fixtures unless the user explicitly requests a live paid generation.
