# Contributing to Game Night Bot

Thank you for considering a contribution. Bug reports, documentation
improvements, testing on different hosting platforms and code changes are all
welcome.

By participating, you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before opening an issue

- Search the existing issues and discussions first.
- Use Discussions for setup help, questions and early ideas.
- Use Issues for reproducible bugs or clearly defined improvements.
- Remove bot tokens, API keys, Discord IDs and private server information from
  screenshots and logs.
- Never upload `.env`, `config.json`, `database/games.db`, backups or cached
  artwork.

Security vulnerabilities must follow [SECURITY.md](SECURITY.md) and must not
be posted publicly.

## Development setup

1. Fork the repository.
2. Clone your fork.
3. Create a branch from `main`:

   ```console
   git checkout -b feature/short-description
   ```

4. Install Python 3.13 or newer and the project dependencies:

   ```console
   python -m pip install -r requirements.txt
   ```

5. Copy the example configuration only if your change needs a live test:

   ```powershell
   Copy-Item .env.example .env
   Copy-Item config.example.json config.json
   ```

6. Use a separate Discord test server and a test bot token. Do not run
   development code against a production community or production database.

Most unit tests do not require real Discord, IGDB or SteamGridDB credentials.

## Project expectations

- Keep changes focused. Unrelated refactors should use a separate pull
  request.
- Preserve the shared HTTP session and shared SQLite connection architecture.
- Consider small hosting plans: avoid retaining rendered cards, artwork or GIF
  frames in memory longer than necessary.
- Do not add a new third-party dependency unless it provides a clear benefit.
- Do not add telemetry, advertising or external data collection.
- Continue to support a single self-hosted bot instance for one Discord server
  unless a larger architectural change has been discussed first.
- When adding manual game-metadata overrides, cite an authoritative source in
  the pull request.
- Keep user-facing error messages understandable and avoid exposing secrets in
  logs.

## Tests and checks

Before opening a pull request, run:

```console
python -m compileall bot.py settings.py commands database ui utils
python -m unittest discover -s tests -v
```

Add or update tests when changing database behaviour, metadata parsing,
release-date handling, wheel selection or caching.

Manual Discord testing is helpful for changes involving embeds, buttons,
permissions or animations. Include a screenshot or short recording in the
pull request when the visual result matters.

## Commit and pull-request guidance

- Write a clear commit message describing the change.
- Explain what problem the pull request solves.
- Link the related issue when one exists.
- List the checks you ran and their results.
- Mention any database migration, configuration change or new environment
  variable.
- Keep secrets and runtime data out of commits.

Pull requests may be asked to make changes before merging. Maintainers may
close proposals that conflict with the project's scope, security requirements
or resource limits, but constructive alternatives are always welcome.

