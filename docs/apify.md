# Running searches on Apify

The `apify` source runs the same navigation the local backend does, but in a
container on Apify's infrastructure instead of on your machine.

**Read this whole page before turning it on.** It is not the obvious win it
sounds like, and the local backend is the default for good reasons.

## What you are actually getting

The honest version:

| | `local` | `apify` |
| --- | --- | --- |
| Setup | `pip install` | Apify account, token, deployed actor |
| Cost | free | compute units + residential proxy |
| Your Naukri session | stays on your disk | uploaded to Apify |
| Speed | one browser, sequential | one browser, sequential |
| Runs unattended | only while your machine is on | yes |
| Blocked by Akamai | sometimes | more often |

The one real advantage is the last-but-one row: a scheduled Apify run happens
whether or not your laptop is open. If you do not need that, use `local`.

## Three constraints that surprise people

**1. It cannot be a cheap HTTP scraper.**
Naukri renders results from `/jobapi/v3/search`, and that endpoint is
request-signed — it wants an `nkparam` header and a bearer token minted by the
page itself. A plain `requests.get` returns 406. So the actor runs a full
browser and pays for one page load per result set. Apify pricing assumes cheap
scrapers; this is not one.

**2. It has to run headed.**
Akamai serves "Access Denied" to headless Chromium. The actor's Dockerfile
starts Xvfb and launches a headed browser against a virtual display. This is
why the image is `apify/actor-python-playwright` and the command is
`xvfb-run -a python main.py`.

**3. Datacenter proxies do not work.**
Apify's default proxy group is datacenter, and those IP ranges are on every
bot-protection blocklist. You need `RESIDENTIAL`, which is the expensive tier.
The actor defaults to it; if you override the proxy config, keep it.

## The session question

Recommendations and several useful listing fields need a logged-in session, so
the actor takes your Playwright storage state as input.

That storage state **is your Naukri login**. Anyone holding it is signed in as
you, without needing your password or an OTP. Sending it to Apify means:

- it sits in that actor's key-value store, private to your account
- Apify staff and anyone with access to your Apify account can read it
- it is one more place a breach can reach

`input_schema.json` marks it `isSecret`, which keeps it out of the run log and
the console UI. That is real but it is not encryption.

If that trade is not one you want, either:

```yaml
apify:
  upload_session: false     # runs signed out - fewer results, no recommendations
```

or use `source: local` and keep the session on your own disk.

Naukri sessions expire every few weeks. When the actor starts returning nothing,
re-run `--login` locally and the next scan uploads the fresh state.

## Setup

**1. Get a token.** [console.apify.com](https://console.apify.com) → Settings →
Integrations → API token.

**2. Deploy the actor.**

```bash
npm install -g apify-cli
apify login
cd apify_actor
apify push
```

That prints your actor's full name, e.g. `yourname/naukri-search`.

**3. Point the screener at it.**

```yaml
source: apify
apify:
  actor: yourname/naukri-search
  country: IN
  timeout_sec: 900
```

Keep the token in the environment rather than the config file:

```bash
# PowerShell
$env:APIFY_TOKEN = "apify_api_..."

# bash
export APIFY_TOKEN="apify_api_..."
```

`config.yaml` is gitignored, but an environment variable cannot be committed by
accident at all.

**4. Run it.**

```bash
python main.py --scan --source apify
```

The run URL is printed as it starts, so you can watch it in the Apify console.

## When it returns nothing

In rough order of likelihood:

1. **Expired session** — re-run `python main.py --login`, then scan again.
2. **Akamai block** — check the actor's screenshot/log in the console. If it
   shows "Access Denied", try a different `country`, or fall back to `local`.
3. **Proxy group** — confirm you are on `RESIDENTIAL`, not datacenter.
4. **Genuinely empty searches** — run `python main.py --check` and read the
   search list. Narrow queries in a small city return very little.

## Scheduling

Apify's own scheduler runs the actor, but the actor only collects — scoring
happens locally. To get a fully unattended pipeline you need the scan running
somewhere too (a cheap VPS, a GitHub Action on a cron, your own box with Task
Scheduler). The actor alone gives you a dataset, not a shortlist.
