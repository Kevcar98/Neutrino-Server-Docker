# Neutrino Server

Your own **cloud music library** for the **Neutrino** app. A small self-hosted
server that stores your music and serves it back to the app — upload from the
phone, browse folders as playlists, stream, and delete. One `docker compose up`.

It only ever serves **your own files**. No search engines, no extraction, nothing
fetched from anywhere — just your library on a box you control.

Takes ~20–30 minutes on a free Oracle Cloud server, no ongoing cost.

## Quick choice: HTTPS (with domain) or plain HTTP (IP only)?

| Setup | Domain required? | HTTPS? |
|-------|------------------|--------|
| **HTTPS** (recommended) | Yes — free ones work (DuckDNS, Cloudflare) | Yes, automatic |
| **Plain HTTP** (IP only) | No — just your server IP | No |

- **Have a domain or want a free one?** Follow **[HTTPS setup](#https-setup-with-domain)** (steps 1–9).
- **Just IP, LAN or testing?** Skip to **[Plain HTTP setup](#plain-http-setup-no-domain-ip-only)**.

---

## HTTPS setup (with domain)

### 1. Create a free Oracle Cloud server

1. Sign up at <https://www.oracle.com/cloud/free/> (a card is required for identity;
   Always Free resources don't charge).
2. **Compute → Instances → Create instance.**
   - **Image:** Canonical **Ubuntu 22.04**.
   - **Shape:** *Ampere* → **VM.Standard.A1.Flex** → **1 OCPU / 6 GB** is plenty
     (still Always Free). If Ampere is unavailable in your region, the AMD
     **VM.Standard.E2.1.Micro** also works.
   - **SSH keys:** upload your public key (or let it generate + download one).
3. Create it, and note the instance's **public IP**.

### 2. Connect to it (PuTTY)

1. Download **PuTTY** (and **PuTTYgen**, bundled with it): <https://www.putty.org/>
2. Oracle's key download is an OpenSSH key — PuTTY needs its own `.ppk` format,
   so convert it first:
   - Open **PuTTYgen** → **Conversions → Import key** → pick the private key
     Oracle gave you.
   - **Save private key** → save as e.g. `oracle-key.ppk`.
3. Open **PuTTY**:
   - **Host Name:** `ubuntu@<instance-public-ip>`
   - **Port:** 22
   - Left tree → **Connection → SSH → Auth → Credentials** → **Private key
     file for authentication** → browse to `oracle-key.ppk`.
   - (Optional) Back in **Session**, save it under **Saved Sessions**.
4. **Open** → accept the host key prompt on first connect → you're in.

Run everything below **inside that PuTTY session**.

### 3. Open the firewall (two layers — both needed)

**Cloud side:** in the Oracle console → your instance's VCN → subnet → Security
List → add **Ingress** rules, source `0.0.0.0/0`:

| Port | Purpose |
|-----:|---------|
| 22   | SSH |
| 80   | HTTP (cert issuing) |
| 443  | HTTPS |
| 8091 | Library server (plain HTTP variant) |

**Inside the VM** (Oracle images ship a locked-down firewall too):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

### 4. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
exit
```

Reconnect (reopen PuTTY, same saved session), then confirm:

```bash
docker --version && docker compose version
```

### 5. Point your domain at it

If you don't have a domain, get any cheap one (or a free one from Duck DNS /
Cloudflare). Add one A/AAAA record → your instance's public IP:

```
library.<your-domain>
```

DNS has to resolve **before** you start the stack, or the free HTTPS certificate
step will fail.

### 6. Get this repo onto the server (git)

Clone it straight from GitHub in your PuTTY session — no file-transfer tool needed:

```bash
sudo apt update && sudo apt install -y git
cd ~
git clone https://github.com/Kevcar98/Neutrino-Server-Docker.git neutrino-server
cd neutrino-server
```

It's a public repo, so no login. To update later: `cd ~/neutrino-server && git pull && docker compose up -d --build`.

### 7. Configure it

```bash
cd ~/neutrino-server

cp .env.example .env
nano .env                  # set DOMAIN to your real domain
```

*(Optional)* Put your own music in `local/library/music/` now, so your library
shows up right away — otherwise it starts empty and you upload from the app later.

### 8. Start it

```bash
docker compose up -d
docker compose logs -f caddy library
```

Give it a minute for the HTTPS certificate, then check:

```bash
curl "https://library.<your-domain>/health"      # {"status":"ok",...}
curl "https://library.<your-domain>/playlists"   # your folders
```

### 9. Add it to the app

In Neutrino: **Settings → My Server** → **Host:** `https://library.<your-domain>`
→ **Save**. Your library, uploads, and playback now route through your own server.

---

## Plain HTTP setup (no domain, IP only)

A complete, standalone walkthrough — no domain, just your server's IP on port 8091.

### 1. Create a free Oracle Cloud server

1. Sign up at <https://www.oracle.com/cloud/free/> (a card is required for identity;
   Always Free resources don't charge).
2. **Compute → Instances → Create instance.**
   - **Image:** Canonical **Ubuntu 22.04**.
   - **Shape:** *Ampere* → **VM.Standard.A1.Flex** → **1 OCPU / 6 GB** is plenty
     (still Always Free). If Ampere is unavailable in your region, the AMD
     **VM.Standard.E2.1.Micro** also works.
   - **SSH keys:** upload your public key (or let it generate + download one).
3. Create it, and note the instance's **public IP**.

### 2. Reserve your public IP (don't skip this)

Oracle's default public IP is **ephemeral** — it can change if the instance stops
and restarts, breaking your URL. Make it permanent, still free:

1. Oracle console → **Networking → IP Management → Reserved Public IPs**.
2. **Create Reserved Public IP.**
3. Instance → **Attached VNICs** → the VNIC → **IPv4 addresses** → edit the
   public IP → switch **Ephemeral** to the **Reserved** IP you just made.

### 3. Connect to it (PuTTY)

1. Download **PuTTY** (and **PuTTYgen**, bundled with it): <https://www.putty.org/>
2. Oracle's key download is an OpenSSH key — PuTTY needs its own `.ppk` format,
   so convert it first:
   - Open **PuTTYgen** → **Conversions → Import key** → pick the private key
     Oracle gave you.
   - **Save private key** → save as e.g. `oracle-key.ppk`.
3. Open **PuTTY**:
   - **Host Name:** `ubuntu@<your-reserved-ip>`
   - **Port:** 22
   - Left tree → **Connection → SSH → Auth → Credentials** → **Private key
     file for authentication** → browse to `oracle-key.ppk`.
   - (Optional) Back in **Session**, save it under **Saved Sessions**.
4. **Open** → accept the host key prompt on first connect → you're in.

Run everything below **inside that PuTTY session**.

### 4. Open the firewall (two layers — both needed)

For plain HTTP the only port you need open is **8091**.

**Cloud side:** in the Oracle console → your instance's VCN → subnet → Security
List → add **Ingress** rules, source `0.0.0.0/0`:

| Port | Purpose |
|-----:|---------|
| 22   | SSH |
| 8091 | Library server |

**Inside the VM** (Oracle images ship a locked-down firewall too):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8091 -j ACCEPT
sudo netfilter-persistent save
```

### 5. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
exit
```

Reconnect (reopen PuTTY, same saved session), then confirm:

```bash
docker --version && docker compose version
```

### 6. Get this repo onto the server (git)

```bash
sudo apt update && sudo apt install -y git
cd ~
git clone https://github.com/Kevcar98/Neutrino-Server-Docker.git neutrino-server
cd neutrino-server
```

It's a public repo, so no login. To update later: `cd ~/neutrino-server && git pull && docker compose -f docker-compose.http.yml up -d --build`.

### 7. Start it

No `.env` needed for plain HTTP.

```bash
docker compose -f docker-compose.http.yml up -d
docker compose -f docker-compose.http.yml logs -f library
```

Check (locally on the server):

```bash
curl "http://localhost:8091/health"      # {"status":"ok",...}
curl "http://localhost:8091/playlists"   # folders
```

### 8. Add it to the app

In Neutrino: **Settings → My Server** → **Host:** `http://<your-reserved-ip>:8091`
→ **Save**. Your library, uploads, and playback now route through your own server.

---

## Adding music

- Drop files (or whole folders — each folder becomes a playlist) into
  `local/library/music/` on the server, then hit **rescan** from the app or
  `curl -X POST .../rescan`.
- Or upload straight from the app — anything you save to the server lands here.
- Cover art/artist come from the files' own tags; missing art is filled in from
  the free iTunes lookup (set `LIBRARY_ONLINE_ENRICH=0` to stay fully offline).

## Notes

- This is **your own** server — nobody else needs to run anything, and nobody
  can see or use it unless you give them the URL.
- Keep `.env` private (holds your domain config). Your music stays on the server;
  it's git-ignored so it never gets committed.

## License

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Neutrino Server is Free Software: you can use, study, share, and improve it at
will. Specifically you can redistribute and/or modify it under the terms of the
[GNU General Public License](https://www.gnu.org/licenses/gpl-3.0.html) as
published by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version. Full text: [LICENSE](LICENSE).

The library server links **mutagen** (GPL-2.0-or-later); everything else (FastAPI,
uvicorn, httpx) is permissive (MIT/BSD) and GPL-compatible.
