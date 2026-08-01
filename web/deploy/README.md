# Deploying on the Mac mini (free hosting via Cloudflare Tunnel)

The Mac mini runs both processes under launchd (auto-start at boot, auto-restart on
crash), and Cloudflare Tunnel exposes them as `https://yourdomain.com` with no open
ports, no port forwarding, and no exposed home IP. Only cost: the domain (~€10/yr).

## 1. Domain + Cloudflare (one-time, ~15 min)

1. Create a free Cloudflare account and either buy a domain there (at-cost pricing)
   or point an existing domain's nameservers at Cloudflare.
2. Install and authenticate the tunnel client:
   ```bash
   brew install cloudflared
   cloudflared tunnel login          # opens browser, pick your domain
   cloudflared tunnel create inbox-labeler
   cloudflared tunnel route dns inbox-labeler yourdomain.com
   ```
3. Copy `web/deploy/cloudflared-config.yml` to `~/.cloudflared/config.yml`, replacing
   `TUNNEL_ID` (printed by `tunnel create`, also visible via
   `cloudflared tunnel list`) and `yourdomain.com`.
4. Install it as a service (survives reboots):
   ```bash
   sudo cloudflared service install
   ```

## 2. App + worker under launchd

```bash
# Update BASE_URL in web/.env first:  BASE_URL=https://yourdomain.com
cp web/deploy/com.skan.inbox-labeler-web.plist ~/Library/LaunchAgents/
cp web/deploy/com.skan.inbox-labeler-worker.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.skan.inbox-labeler-web.plist
launchctl load ~/Library/LaunchAgents/com.skan.inbox-labeler-worker.plist
```

Both use `KeepAlive` — launchd restarts them if they crash and starts them at login.
Logs: `web/server.log`, `web/worker.log` (+ `.err.log` siblings).

To stop / restart after a code change:
```bash
launchctl unload ~/Library/LaunchAgents/com.skan.inbox-labeler-web.plist && \
launchctl load  ~/Library/LaunchAgents/com.skan.inbox-labeler-web.plist
# same for ...-worker.plist
```

## 3. Point the external services at the domain

- **Google Cloud console** → your Web OAuth client → add authorized redirect URI
  `https://yourdomain.com/oauth/callback`. Consent screen → set the privacy policy
  URL to `https://yourdomain.com/privacy`.
- **Stripe dashboard** → Developers → Webhooks → add endpoint
  `https://yourdomain.com/stripe-webhook` (event: `checkout.session.completed`);
  copy its `whsec_...` into `web/.env` as `STRIPE_WEBHOOK_SECRET` (replaces the
  stripe-CLI one used locally).

## 4. Keep the mini awake

System Settings → Energy → enable "Prevent automatic sleeping when the display is
off" (likely already set for the personal labeler). Optionally
`sudo pmset -a sleep 0`.

## Checks

```bash
launchctl list | grep inbox-labeler       # both listed, exit code 0
curl -s https://yourdomain.com/ | head    # landing page over the tunnel
tail -f web/worker.log                    # worker heartbeat / job runs
```

## When to move off the mini

Self-hosting rides on your home connection: if the mini or your internet is down,
checkout is down. Fine while validating with ≤100 test users. When strangers pay
daily, move to a small VPS (~€4/mo Hetzner) — the deploy is identical minus the
tunnel (or keep the tunnel; cloudflared runs anywhere).
