# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report them privately instead: on GitHub, go to **Security → Report a vulnerability**
(private vulnerability reporting). Include steps to reproduce and the impact.
You'll get an answer within a few days.

## Scope

GGTA runs locally and handles sensitive things:

- API keys in `clipsmith/.env` (Upload-Post, LLM providers, Twitch)
- Browser cookies, when `CLIPSMITH_COOKIES_BROWSER` is set for age-restricted YouTube videos
- OAuth tokens for publishing

Bugs that could leak these, run untrusted input as commands, or publish
content without the user's intent are in scope.

## Good practices for users

- Never commit `.env`. It's in `.gitignore`.
- Use a dedicated Upload-Post profile per brand, and revoke keys you no longer use.
- Only set `CLIPSMITH_COOKIES_BROWSER` on a machine you trust.
