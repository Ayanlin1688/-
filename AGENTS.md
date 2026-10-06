# Yanlin Smart-Creation Matrix project workflow

The user authorizes incremental work on `main` in this project and an automatic push to the user's own repository after each completed, verified logical code change. Do not ask again for permission to perform that commit/push.

- The only remote is `origin` = `https://github.com/Ayanlin1688/-.git`. Never add, re-create, or push to a `legacy`/mirror remote; the old third-party repository `admin11044/StoryboardVideoStudio` must never receive a push from this project. Never replace `origin` and never force-push.
- Commit identity is fixed to `Ayanlin1688 <329457281+Ayanlin1688@users.noreply.github.com>`. Before committing, verify `git config user.email`; never change `user.name`/`user.email` to anything else, never use `--author` or temporary identity overrides, and never log into or store credentials/tokens for any other GitHub account such as admin11044.
- Preserve existing work and run relevant tests before committing. Use Chinese commit descriptions with `feat:`, `fix:`, `refactor:` or `docs:`.
- Run `python -X utf8 scripts/sync_github.py --message-file <UTF-8 message file>` after verification. It commits once, pushes only to `origin`, verifies `refs/heads/main` on origin, and retries only a failed push up to 3 attempts without creating another commit.
- Confirm origin's `main` equals local HEAD. After a successful push tell the user: `已同步到GitHub，最新commit: <hash>`, using the full hash, and also report the author via `git log -1 --format="%an <%ae>"`.
- Never commit config.json, API keys, tokens, environment files, generated videos, execution logs, screenshots, or local real-material diagnostic artifacts. Keep .gitignore protections and secret checks intact. Credentials belong in the local config or Git Credential Manager, never in code/remote URLs/chat.
- If authentication/network blocks a push, retain the local commits, complete independent authorized work, and report the actual blocker. Do not claim remote sync succeeded.
- Keep the existing PyQt5 + QFluentWidgets stack; do not mix Qt bindings. Tests use isolated configuration and local HTTP fixtures unless the user explicitly requests a live paid generation.
