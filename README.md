# Neutrino Server — easy install (with YouTube)

The self-hosted backend for the **Neutrino** music player app. One self-contained
server: your own music library + search/streaming + YouTube, all in one
`docker compose up`. Clone or unzip this wherever you're deploying from.

Takes ~20–30 minutes on a free Oracle Cloud server, no ongoing cost.

## Quick choice: HTTPS (with domain) or plain HTTP (IP only)?

| Setup | Domain required? | HTTPS? |
|-------|------------------|--------|
| **HTTPS** (recommended) | Yes — free ones work (DuckDNS, Cloudflare) | Yes, automatic |
| **Plain HTTP** (IP only) | No — just your server IP | No |

- **Have a domain or want a free one?** Follow **HTTPS setup** below (steps 1–9).
- **Just IP, LAN or testing?** Skip to **Plain HTTP setup**.

---

## HTTPS setup (with domain)

### 1. Create a free Oracle Cloud server

1. Sign up at <https://www.oracle.com/cloud/free/> (a card is required for identity;
   Always Free resources don't charge).
2. **Compute → Instances → Create instance.**
   - **Image:** Canonical **Ubuntu 22.04**.
   - **Shape:** *Ampere* → **VM.Standard.A1.Flex** → **2 OCPU / 12 GB** (within
     Always Free). If unavailable in your region, try the AMD
     **VM.Standard.E2.1.Micro** instead (smaller, still free).
   - **SSH keys:** upload your public key (or let it generate + download one).
3. Create it, and note the instance's **public IP**.

### 2. Connect to it (PuTTY)

1. Download **PuTTY** (and **PuTTYgen**, bundled with it):
   <https://www.putty.org/>
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
   - (Optional) Back in **Session**, type a name under **Saved Sessions** →
     **Save**, so you don't redo this every time.
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

Reconnect (reopen PuTTY, same saved session as step 2), then confirm:

```bash
docker --version && docker compose version
```

### 5. Point your domain at it

If you don't have a domain, get any cheap one (or a free one from a provider like
Duck DNS / Cloudflare). Add these A/AAAA records → your instance's public IP:

```
api.<your-domain>
proxy.<your-domain>
library.<your-domain>
ytresolver.<your-domain>
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
   - **Key file:** browse to your private key — either the original one from
     Oracle, or the `.ppk` you made in step 2 (FileZilla accepts both)
3. **Connect.**
4. On the right (remote) side, navigate into `/home/ubuntu/`.
5. On the left (local) side, find this folder on your PC.
6. Drag the whole folder from the left panel to the right panel to upload it.
   Rename it to `backend` on the way if you like — matches the `cd` below.

### 7. Configure it

Back in your PuTTY session:

```bash
cd ~/backend

cp .env.example .env
nano .env                  # set DOMAIN + a strong DB_PASSWORD

nano config.properties     # replace every <your-domain>,
                            # and set the password to match .env
```

*(Optional)* Put your own music in `local/library/music/` before starting it, so
your library shows up right away — otherwise it starts empty and you can add
files later.

### 8. Start it

```bash
docker compose up -d
docker compose logs -f caddy backend library resolver
```

Give it a minute or two for HTTPS certificates, then check:

```bash
curl "https://api.<your-domain>/search?q=test&filter=music_songs"   # JSON
curl "https://library.<your-domain>/playlists"                       # your folders
curl "https://ytresolver.<your-domain>/health"                       # {"status":"ok",...}
```

### 9. Add it to the app

In the Music Player app:

1. **Settings → My Server**
   - **Host:** `https://<your-domain>`
   - Save — this wires up search/streaming and your library at once.
2. **Settings → Sources → Install a source**
   - **Name:** `YouTube`
   - **URL:** `https://ytresolver.<your-domain>`
   - Test → **Install**.

That's it — search now covers your library + YouTube, playback works everywhere,
and downloads/uploads route through your own server.

---

## Plain HTTP setup (no domain, IP only)

### 1. Create and connect to server (same as HTTPS steps 1–2)

Follow **HTTPS steps 1–2** above — create an Oracle instance, connect with PuTTY.

### 2. Reserve your public IP (don't skip this)

Oracle's default public IP is **ephemeral** — it can change if the instance stops
and restarts, breaking every URL you configure below. Make it permanent instead,
still free:

1. Oracle console → **Networking → IP Management → Reserved Public IPs**.
2. **Create Reserved Public IP.**
3. Go to your instance → **Attached VNICs** → the VNIC → **IPv4 addresses** →
   edit the public IP → switch it from **Ephemeral** to the **Reserved** IP you
   just created.
4. Your instance's public IP is now permanent. Use it everywhere below.

### 4. Open firewall (same as HTTPS step 3)

Follow **HTTPS step 3** — both cloud and VM firewall layers.

### 5. Install Docker (same as HTTPS step 4)

Follow **HTTPS step 4**.

### 6. Upload folder and configure

```bash
cd ~/backend
cp .env.example .env
nano .env                  # set strong DB_PASSWORD (DOMAIN line doesn't matter)

nano config.properties.http   # replace every <server-ip> with your
                               # reserved public IP from step 2
```

### 7. Start with plain HTTP

```bash
docker compose -f docker-compose.http.yml up -d
docker compose -f docker-compose.http.yml logs -f backend library resolver
```

Give it 30 seconds, then check:

```bash
curl "http://localhost:8080/search?q=test&filter=music_songs"   # JSON
curl "http://localhost:8091/playlists"                           # folders
curl "http://localhost:9000/health"                              # {"status":"ok",...}
```

### 8. Add it to the app

In the Music Player app, using your **reserved public IP** from step 2:

1. **Settings → My Server**
   - **Host:** `http://<server-ip>` (e.g. `http://140.238.x.x`)
   - Save.
2. **Settings → Sources → Install a source**
   - **Name:** `YouTube`
   - **URL:** `http://<server-ip>:9000`
   - Test → **Install**.

---

## Notes

- This is **your own** server — nobody else needs to be running anything for it
  to work, and nobody else can see or use it unless you give them the URL.
- YouTube can break for a bit whenever they change something; it usually
  self-heals within a day (the resolver auto-updates its extractor on restart).
- Keep `.env` and `config.properties` / `config.properties.http` private — they
  hold your DB password.
