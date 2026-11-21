# Code-scanning triage

| Alert | CodeQL rule | Reported location | Disposition |
| --- | --- | --- | --- |
| 8 | `py/clear-text-storage-sensitive-data` | `hramatka/api/app.py:811` | False positive. The server-generated opaque session credential reaches the browser through the documented host-only `Set-Cookie` header with `Secure`, `HttpOnly`, and `SameSite=Lax`. Issuing responses use `Cache-Control: no-store`. The cookie is sensitive and intentionally sent to the session owner over HTTPS. SQLite receives only the domain-separated digest. |
