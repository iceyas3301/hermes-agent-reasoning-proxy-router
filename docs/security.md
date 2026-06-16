# Security and redaction

This repository is safe to publish because it contains only plugin code and sanitized configuration examples.

Do not commit:

- `.env`
- `auth.json`
- OAuth tokens or API keys
- Telegram chat IDs, user IDs, thread IDs, or bot tokens
- Gateway logs with real message text
- Session transcripts
- Hostnames for private services
- Internal IP ranges unless they are generic examples
- Provider account IDs
- Cloudflare, GitHub, OpenRouter, OpenAI, or other service tokens

Use placeholders instead:

```text
USER_ID
THREAD_ID
example.com
[REDACTED]
```

If you enable `decision_log`, treat the JSONL file as sensitive operational data. It may not contain full messages, but it can still include session keys and routing metadata.

Before publishing, run a secret scan across the repository and inspect matches manually.
