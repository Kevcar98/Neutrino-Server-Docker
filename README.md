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

- **Have a domain or want a free one?** Follow **HTTPS setup** (steps 1–8).
- **Just IP, LAN or testing?** Skip to **Plain HTTP setup**.

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

### 6. Upload this folder to the server (FileZilla)

1. Download **FileZilla Client**: <https://filezilla-project.org/download.php?type=client>
2. **File → Site Manager → New Site.**
   - **Protocol:** SFTP – SSH File Transfer Protocol
   - **Host:** `<instance-public-ip>`
   - **Logon Type:** Key file
   - **User:** `ubuntu`
   - **Key file:** browse to your private key (the original from Oracle or the
     `.ppk` — FileZilla accepts both)
3. **Connect.** On the right (remote) side go into `/home/ubuntu/`, on the left
   find this folder, drag it across. Rename it to `neutrino-server` if you like
   (matches the `cd` below).

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

### 1. Create and connect to server

Follow **HTTPS steps 1–2** — create an Oracle instance, connect with PuTTY.

### 2. Reserve your public IP (don't skip this)

Oracle's default public IP is **ephemeral** — it can change if the instance stops
and restarts, breaking your URL. Make it permanent, still free:

1. Oracle console → **Networking → IP Management → Reserved Public IPs**.
2. **Create Reserved Public IP.**
3. Instance → **Attached VNICs** → the VNIC → **IPv4 addresses** → edit the
   public IP → switch **Ephemeral** to the **Reserved** IP you just made.

### 3. Open firewall

Follow **HTTPS step 3**, but the port you need open is **8091** (not 80/443):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8091 -j ACCEPT
sudo netfilter-persistent save
```

Add the matching **Ingress** rule (port 8091, source `0.0.0.0/0`) on the cloud side too.

### 4. Install Docker

Follow **HTTPS step 4**.

### 5. Upload the folder and start it

Upload as in **HTTPS step 6**. No `.env` needed for plain HTTP.

```bash
cd ~/neutrino-server
docker compose -f docker-compose.http.yml up -d
docker compose -f docker-compose.http.yml logs -f library
```

Check (locally on the server):

```bash
curl "http://localhost:8091/health"      # {"status":"ok",...}
curl "http://localhost:8091/playlists"   # folders
```

### 6. Add it to the app

**Settings → My Server** → **Host:** `http://<server-ip>:8091` (your reserved IP)
→ **Save**.

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
