# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected secret leak, authentication bypass, unsafe file handling, route isolation failure or exposure of private message content.

Use GitHub private vulnerability reporting when it is enabled for the repository. If it is unavailable, contact the repository owner through a private channel listed on the repository profile. Include a minimal reproduction with synthetic data and redact all credentials, chat identifiers and message content.

Do not test against systems, bots, chats, accounts or infrastructure you do not own or have explicit permission to assess.

## Secret handling

Runtime secrets belong only in `~/chat-daily/.env` or the equivalent operator-controlled data directory. They must not appear in:

- Git history or source files;
- README examples, fixtures or screenshots;
- issue/PR text;
- logs or exception URLs;
- build artifacts and archives.

If a real secret is exposed, revoke or rotate it first. Removing the string from the latest commit is not sufficient because Git history, forks, caches and logs may retain it.

## Sensitive data

Treat source messages, Telegram route tables, SQLite databases, archives, media, seen files and ledgers as private operational data. Use synthetic fixtures in tests. A vulnerability report should demonstrate the class of issue without attaching real user data.

## Supported code

This source archive does not contain release tags or a published support matrix. Until maintainers publish one, security fixes should target the current default branch and clearly state whether older deployments are affected. The presence of public source does not imply a managed service or guaranteed response time.
