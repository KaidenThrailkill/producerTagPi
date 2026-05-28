# Notion Webhook Setup — Remaining Tasks

Track-back of what's left to wire the Notion -> Pi sound trigger after the code changes landed.

## 1. Add Isaiah's sound file

- [ ] Drop `sounds/isaiah.m4a` into the `sounds/` directory on the Pi (Josh's sound is already added).
- Alternative: edit [config.yaml](config.yaml) under `notion.users` to point Isaiah's UUID at a different filename you already have.

## 2. Install dependencies on the Pi

```bash
cd /home/pi/producerTagPi
.venv/bin/pip install -r requirements.txt
```

Adds `httpx==0.27.2` for the follow-up Notion API call.

## 3. Get the ngrok public URL

On the Pi:

```bash
curl -s http://127.0.0.1:4040/api/tunnels | python3 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])"
```

Your webhook URL will be that + `/notion`, e.g. `https://1a2b-73-200-12-34.ngrok-free.app/notion`.

If `curl` returns nothing: `sudo systemctl status producertags-ngrok`.

## 4. Confirm the Notion integration is set up

- [ ] Integration exists at https://www.notion.so/profile/integrations
- [ ] Integration token is already in `.env` as `NOTION_INTEGRATION_TOKEN` (done — token starts with `ntn_603106...`)
- [ ] Sprint Backlog database is shared with the integration:
  - Open the Sprint Backlog database in Notion
  - Top-right `...` menu -> **Connections** -> search the integration name -> **Confirm**

## 5. Create the webhook subscription in Notion

In the integration's settings page -> **Webhooks** tab -> **+ Create a subscription**:

- [ ] **Webhook URL**: real ngrok URL from step 3, ending in `/notion`
- [ ] **API version**: `2026-03-11`
- [ ] **Events**: uncheck **Events (28)** to clear, then expand **Page** and check only:
  - `page.properties_updated` (required)
  - `page.created` (optional — covers cases where a task is created with Status already set to Complete)
- [ ] Click **Create subscription**

## 6. Capture the verification token

On the Pi, watch the logs:

```bash
sudo journalctl -u producertags -f
```

Look for:

```
WARNING NOTION VERIFICATION TOKEN — paste this into Notion's UI: secret_AbCdEf123...
```

Copy everything after the colon. The token doubles as both the Notion UI verification value AND the HMAC signing secret.

## 7. Verify the subscription in Notion

- [ ] Back in the Webhooks tab, find the **Pending verification** subscription
- [ ] Click **Verify** and paste the token from step 6 -> confirm

## 8. Add the signing secret to .env on the Pi

```bash
sudo nano /home/pi/producerTagPi/.env
```

Set:

```
NOTION_SIGNING_SECRET=<same token from step 6>
```

Then restart:

```bash
sudo systemctl restart producertags
```

## 9. Test end-to-end

- [ ] In Notion's Sprint Backlog, take a task assigned to Josh Sargent and change Status -> **Complete**. Josh's sound should play within a couple seconds.
- [ ] Repeat with a task assigned to Isaiah Molina (once `sounds/isaiah.m4a` exists).
- [ ] Visit `/dashboard` on the Pi to confirm the event shows up in the event log as `played`.

## Troubleshooting checklist

| Symptom | Likely cause |
|---|---|
| No verification token in logs | Notion can't reach the URL — test `https://<ngrok>/health` in a browser |
| `401 invalid signature` after verification | `NOTION_SIGNING_SECRET` in `.env` does not match the verification token byte-for-byte |
| `fetch_failed` events in dashboard | Integration token wrong, OR database not shared with integration (step 4) |
| `no_sound, reason: no_matching_assignee` | The page's assignee UUID does not match `config.yaml`. Dashboard event log shows the actual assignee IDs — compare to `notion.users` keys in [config.yaml](config.yaml) |
| `no_sound, reason: status_not_done` | Status property is something other than "Complete" — verify spelling and that it's the `Status` property, not a `Select` |
| `no_sound, reason: already_done` | The page was already marked Complete on a prior event — expected behavior, not a bug |

## Known caveats

- **ngrok free-tier URL rotation**: free ngrok URLs change whenever the tunnel restarts. If you reboot the Pi or restart `producertags-ngrok`, the URL flips and Notion's subscription breaks until you update it in Notion's UI. Consider grabbing ngrok's free reserved subdomain.
- **In-memory status cache**: `NOTION_PAGE_STATUS_CACHE` is cleared on app restart. The first event for each page after a restart can't tell whether it's a fresh transition or a re-fire — you may hear at most one extra play per page after each `systemctl restart producertags`.
