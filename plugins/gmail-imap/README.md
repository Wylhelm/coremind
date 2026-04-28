# coremind-plugin-gmail-imap

Provider-agnostic IMAP plugin for Gmail, Fastmail, ProtonMail Bridge, iCloud, etc.

Uses IMAP IDLE for low-latency new-message notifications, emits one
`WorldEvent` per message with `subject`, `sender`, and `has_attachment`
attributes. Bodies are **not** stored in L2 — they belong in L3 semantic
memory (written by a separate ingest step), indexed by `email_id`.

## Configuration

```toml
[imap]
host = "imap.gmail.com"
port = 993
username = "you@example.com"
password_env = "GMAIL_IMAP_PASSWORD"
folder = "INBOX"
```
