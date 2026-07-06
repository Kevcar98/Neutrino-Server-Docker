# YouTube cookies (fixes "Sign in to confirm you're not a bot")

YouTube blocks datacenter IPs (like this Oracle server) unless yt-dlp sends
cookies from a logged-in account.

## Easiest: sign in from the app (no files, no SSH, nothing to configure)

In the app: **Settings → YouTube sign-in → Sign in to YouTube** → log into a
throwaway Google account. The app harvests the cookies and pushes them to the
resolver automatically. Re-tap when YouTube starts blocking again.

Pairing is automatic (trust-on-first-use): the app makes its own token and the
server trusts whichever device pushes first — so **sign in from the app right
after you start the server**, before leaving `/cookies` reachable and unused. To
re-pair a different phone later, delete `cookies/.paired_token` on the server and
sign in again. This folder/file method below is the manual alternative.

## Manual: drop a cookies.txt here

The resolver also picks up a `cookies.txt` placed in this folder automatically.

## Export cookies.txt

1. **Use a throwaway / secondary Google account** — not your main one. Running a
   session from a datacenter IP can get an account rate-limited or flagged.
2. In Chrome/Firefox, install the extension **"Get cookies.txt LOCALLY"**.
3. Log into <https://www.youtube.com> with that account.
4. Open a **new incognito/private window**, log in there, then — *without logging
   out* — click the extension and **Export** cookies for `youtube.com` in
   **Netscape** format. (Incognito avoids the browser later rotating/invalidating
   the cookies you just exported.)
5. Save the file as `cookies.txt` in this folder (`~/backend/cookies/cookies.txt`
   on the server — upload via FileZilla).

## Apply

```bash
cd ~/backend
docker compose restart resolver
```

Test — this should now return audio JSON instead of a 502:

```bash
curl -X POST http://localhost:9000/ \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","downloadMode":"audio"}'
```

## Note

Cookies expire (days to weeks). When YouTube starts blocking again, re-export
and replace `cookies.txt`, then `docker compose restart resolver`.
