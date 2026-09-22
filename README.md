# a2a — Agent2Agent servers for public data

Nine A2A servers, one shared stdlib-only kit. Each server publishes a real agent card,
speaks A2A 0.3.0 JSON-RPC, streams task updates over SSE, and POSTs webhook
notifications when the public data it watches changes.

Everything runs on public open data. No API keys, no scraping, no fake fixtures in
production paths.

| Server | Agent card | Skills | Novelty |
|---|---|---|---|
| [`servers/nyc311`](servers/nyc311) | NYC Open Data Agent | `complaint-status`, `complaints-near` | First city government A2A server (public agent card for 311 data) |
| [`servers/nycflood`](servers/nycflood) | NYC Street Flooding Agent | `flood-recent`, `flood-sensors`, `flood-watch` | First street-flooding sensor agent; the only municipal dataset here that is genuinely event-shaped, which makes the push notification a real alert |
| [`servers/nycwater`](servers/nycwater) | NYC Drinking Water Agent | `water-quality`, `water-sites`, `water-watch` | First drinking-water-quality agent of any kind (172K DEP distribution samples, published as monitoring-site codes) |
| [`servers/nws`](servers/nws) | NWS Weather Alerts Agent | `alerts-active`, `alerts-summary`, `alerts-watch` | First National Weather Service agent: active alerts by state, point or severity, straight from `api.weather.gov` |
| [`servers/quakes`](servers/quakes) | USGS Earthquake Agent | `quakes-recent`, `quakes-near`, `quakes-summary`, `quakes-watch` | First earthquake agent on the USGS catalog — worldwide or within N km of any point, with a magnitude-threshold watch |
| [`servers/tides`](servers/tides) | NOAA Tides Agent | `tide-predictions`, `tide-next`, `water-level`, `stations`, `tide-watch` | First tide agent: NOAA CO-OPS high/low predictions and observed water levels for 3,499 stations, placed against published flood stages, with a crossing watch |
| [`servers/recalls`](servers/recalls) | openFDA Recalls Agent | `recalls-recent`, `recall-search`, `recalls-summary`, `recall-lookup`, `recalls-watch` | First product-recall agent: openFDA drug, food and device enforcement reports (87,000+ records), with counts by classification and a new-recall watch |
| [`servers/aurora`](servers/aurora) | Aurora Space Weather Agent | `aurora-now`, `aurora-forecast`, `aurora-visibility`, `aurora-messages`, `aurora-watch` | First space-weather agent: NOAA SWPC's Kp index and storm messages, plus the OVATION model's aurora probability at any point, with a storm watch |
| [`servers/fire`](servers/fire) | Wildfire Incident Agent | `fire-active`, `fire-near`, `fire-summary`, `fire-lookup`, `fire-watch` | First wildfire agent: the interagency WFIGS incident layer (active fires, acreage, containment, real distances), with a new-large-fire watch |

Public A2A servers today are almost all crypto bots, dev tooling, and B2B AI shops.
No city, water utility, weather service, geological survey, oceanographic service, food
safety regulator, space-weather center, land-management agency, transit agency, or
hospital publishes an agent card. These nine take
the first slots in that gap — see [`docs/gap-research.md`](docs/gap-research.md) for the
survey behind that claim (and its caveats).

## Quick start

```bash
# terminal 1 — a server (each defaults to its own port)
python3 servers/nyc311/server.py        # http://127.0.0.1:8787
python3 servers/nycflood/server.py      # http://127.0.0.1:8788
python3 servers/nycwater/server.py      # http://127.0.0.1:8789
python3 servers/nws/server.py           # http://127.0.0.1:8791  (set NWS_USER_AGENT)
python3 servers/quakes/server.py        # http://127.0.0.1:8792
python3 servers/tides/server.py         # http://127.0.0.1:8793
python3 servers/aurora/server.py        # http://127.0.0.1:8794
python3 servers/fire/server.py          # http://127.0.0.1:8795
python3 servers/recalls/server.py       # http://127.0.0.1:8796

# terminal 2 — optional: watch pushes arrive
python3 tools/webhook_receiver.py --port 8799

# terminal 3 — read the card, ask a question
curl -s localhost:8787/.well-known/agent-card.json | python3 -m json.tool
curl -s localhost:8787/ -H 'Content-Type: application/json' -d '{
  "jsonrpc":"2.0","id":"1","method":"message/send",
  "params":{"message":{"kind":"message","role":"user","messageId":"m1",
  "parts":[{"kind":"text","text":"Which streets flooded in the last 30 days?"}]}}}' | python3 -m json.tool
```

Ask that second question against port **8788** to see live FloodNet results.

## Protocol coverage

| A2A feature | Where it lives |
|---|---|
| Agent card at `/.well-known/agent-card.json` (plus legacy `agent.json`) | `a2a_kit/httpd.py` |
| `message/send` with `returnImmediately` (background task + polling) | `a2a_kit/protocol.py` |
| `message/stream` — SSE: task, status-update, artifact-update | `a2a_kit/httpd.py` |
| Task lifecycle: submitted → working → completed / failed / input-required / canceled | `a2a_kit/protocol.py`, `a2a_kit/store.py` |
| Multi-turn `input-required` continuation in one task | `a2a_kit/protocol.py` (`_merge`) |
| `tasks/get` (with `historyLength`), `tasks/cancel`, `tasks/resubscribe` | `a2a_kit/protocol.py` |
| `tasks/pushNotificationConfig/{set,get,list,delete}` | `a2a_kit/protocol.py` |
| Server-initiated push: poll the dataset, POST a `status-update` event | `a2a_kit/push.py` |
| Error codes per the official SDK map (-32001 … -32603) | `a2a_kit/errors.py` |
| SQLite persistence for tasks and push configs | `a2a_kit/store.py` |

## Reading the code

```
a2a_kit/          the reusable server kit (stdlib only, no dependencies)
  errors.py       error codes + the SkillAgent contract every server implements
  protocol.py     JSON-RPC methods, task lifecycle, streaming, push configs
  store.py        SQLite task + push-config store
  push.py         the watcher thread that fires webhooks
  httpd.py        agent card, HTTP routes, SSE writer
  jsonapi.py      keyless JSON API client base: caching, validation, freshness
  socrata.py      Socrata/SODA client built on jsonapi: SOQL escaping, row ids
  cli.py          one CLI for every server
  smoke.py        shared smoke-check helper
servers/<name>/   data.py (dataset client) · agent.py (skills) · server.py (main)
                  tests/ · tools/smoke.py · .env.example
tests/test_kit.py tests for the kit itself
tools/            demo webhook receiver
docs/             the gap research behind the "first of its kind" claims
```

A server is three files: a dataset client, a `SkillAgent` subclass (parse a message,
run a skill, probe a watch), and a four-line `server.py`. The kit does the rest.

## Tests

```bash
python3 tests/test_kit.py
python3 servers/nyc311/tests/test_agent.py
python3 servers/nycflood/tests/test_agent.py
python3 servers/nycwater/tests/test_agent.py
python3 servers/nws/tests/test_agent.py
python3 servers/quakes/tests/test_agent.py
python3 servers/tides/tests/test_agent.py
python3 servers/aurora/tests/test_agent.py
python3 servers/fire/tests/test_agent.py
python3 servers/recalls/tests/test_agent.py
```

307 unit tests: task lifecycle, streaming, push-config validation, webhook delivery
and retries, dataset validation, SOQL escaping, skill parsing, and one full
in-process HTTP + SSE test.

Live end-to-end smoke (hits the real NYC Open Data APIs):

```bash
# terminal 1
NYC311_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/nyc311/server.py
# terminal 2
python3 servers/nyc311/tools/smoke.py
```

202 checks across the nine smoke scripts: card, real lookup, `input-required`
continuation, SSE stream, push-config CRUD. One command runs all nine:

```bash
python3 tools/smoke_all.py
```

## Configuration

Every server reads `.env.example` in its own directory (or real env vars). Each has
its own prefix so the three can run side by side:

| Variable | Purpose |
|---|---|
| `A2A_HOST` / `A2A_PORT` / `A2A_PUBLIC_URL` | Where the server listens and what the card advertises |
| `A2A_PROVIDER_ORG` / `A2A_PROVIDER_URL` | Optional `provider` block in the card |
| `<PREFIX>_APP_TOKEN` | Optional Socrata app token (raises rate limits; not required) |
| `<PREFIX>_CACHE_TTL`, `<PREFIX>_HTTP_TIMEOUT` | Dataset caching and HTTP timeout |
| `<PREFIX>_DB` | SQLite path for tasks + push configs |
| `<PREFIX>_WATCH_INTERVAL` | Seconds between webhook watches (floor: 5) |
| `<PREFIX>_ALLOW_PRIVATE_WEBHOOKS` | `1` allows loopback webhook URLs — local demos only |

Prefixes: `NYC311`, `NYC_FLOOD`, `NYC_WATER`, `NWS`, `USGS`, `NOAA_TIDES`, `AURORA`, `FIRE`,
`OPENFDA`.

`NWS_USER_AGENT`, `USGS_USER_AGENT` and `NOAA_TIDES_USER_AGENT` matter: those agencies
ask that callers identify themselves with a contactable address, and NWS returns 403
without one. SWPC and NIFC are happy keyless, so `AURORA_USER_AGENT` and `FIRE_USER_AGENT`
are good manners rather than requirements. `OPENFDA_API_KEY` is optional — openFDA works keyless and a key only
raises the rate limit to 240 requests/minute.

NOAA's tide server also reads `NOAA_TIDES_UNITS` (feet or metres), `NOAA_TIDES_DATUM`
(the reference level heights are measured from, default MLLW) and
`NOAA_TIDES_STATION_TTL` (the 2 MB station catalogue cache, default one day).

## Honest limits

- **A dry result is not proof of no flooding.** FloodNet publishes completed events,
  so every flood answer states the window it looked at.
- **Water results are per monitoring site, not per address.** DEP publishes site codes
  without coordinates; the agent says so instead of guessing.
- **311 is read-only.** There is no public API to *file* a complaint or pay a ticket;
  the agent links and reports, never pretends.
- **Private webhook URLs are blocked by default.** Allow them only for local demos.
  Hostname webhooks are not DNS-resolved before use, so a determined caller could
  point one at an internal name — put these servers behind an egress firewall.
- **Alerts and quakes are read, not predicted.** The NWS endpoint returns what is
  *currently in effect* and drops alerts the moment they expire, so an empty answer is
  not a forecast. USGS is a catalog of what already happened.
- **Tide answers are NOAA's predictions, not measurements.** `tide-predictions` and
  `tide-next` repeat NOAA's astronomical predictions; `water-level` is the observed
  reading and says whether NOAA has verified it yet. Height datums are named in every
  answer because 2 ft over MLLW means nothing without the datum.
- **Aurora answers are model output, not sightings.** `aurora-visibility` reports NOAA's
  OVATION probability of aurora *overhead* at a point, with the model run time; it says
  nothing about clouds, so every answer tells the caller to check a weather forecast too.
  The Kp forecast beyond about a day is low-confidence by NOAA's own account, and the
  city coordinates for named places come from the server's own list, not from NOAA —
  pass a latitude/longitude for an exact point.
- **Wildfire acreage is what agencies reported, not what a satellite sees.** WFIGS holds
  only incidents that are still active, so a fire leaving the list means it closed out,
  not that it never existed; `fire-near` distances are great-circle miles to the reported
  point and are not a risk assessment or an evacuation notice.
- **openFDA records are unvalidated.** The agency's own disclaimer travels in every
  recall answer, recalls can be corrected or withdrawn after publication, and an empty
  result means "nothing published matches" — not "nothing is happening".
- **Rate limits.** No app token means light use only; the kit caches per query and
  the watcher polls on a timer rather than per request.

## Deploying

These are plain `http.server` apps: fine behind a reverse proxy on a private network,
not hardened for direct public exposure. Set `A2A_PUBLIC_URL` to the real external
URL (the card embeds it), terminate TLS at the proxy, and keep
`<PREFIX>_ALLOW_PRIVATE_WEBHOOKS=0`.

## License

MIT — see [LICENSE](LICENSE).
