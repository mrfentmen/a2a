# Agent Protocol Gap Research — What Is Not Built Yet

**Research run:** 2026-09-22
**Scope:** ACP (Agent Client Protocol), A2A (Agent2Agent Protocol), and where they meet MCP — what exists today vs. what nobody has shipped.
**Method:** Read the primary registries and directories (not blog summaries), then searched for the closest existing thing for every candidate gap. Every "closest existing" claim below is sourced in §6.
**Feeds:** This list is gap material for `ABSENCE-PROTOCOL.md` (the `compiles_to` targets there map to the **Shape** column here).

## Confidence legend

| Mark | Meaning |
|---|---|
| 🟢 | **Verified absent.** Checked the official registry/directory lists + multiple searches on 2026-09-22. Nothing found. |
| 🟡 | **Not found.** Searched, nothing surfaced. Absence cannot be *proven* — a private/unindexed deployment could exist. |
| 🔴 | **Partial.** Something adjacent exists; the gap is the narrow slice described. |

**Shape** column = what you'd build to close it: `A2A` server, `ACP` agent, `MCP` tool, `SKILL`, `LIB` (library/SDK).

---

## 0. TL;DR — the top 12 (best confidence × buildability)

1. 🟢 **The official A2A registry.** The LF roadmap literally still lists "consolidation of efforts for registry" as upcoming. Only community attempts exist: a2a-registry.org, sing1ee/a2a-directory, discussion #924. (`LIB`/`A2A`)
2. 🟢 **Non-coding ACP agents.** The ACP Registry (40+ agents); every single one is a coding or engineering agent (one marketplace exception). No Excel, CAD, Blender, DAW, GIS, video, or research agent has an ACP face. (`ACP`)
3. 🟢 **City / municipal A2A servers.** Boston ships MCP agents over open data; the GSA runs an *MCP* hackathon; Salesforce sells closed city agents. No city publishes an Agent Card. (`A2A`)
4. 🟢 **Travel / transportation A2A servers.** awesome-a2a's Travel & Transportation section is literally empty, and the top 2026 article on the topic is titled "Why Can't Your AI Agent Book a Flight?" (`A2A`)
5. 🟢 **Knowledge services A2A** — awesome-a2a's Knowledge Services section is empty too. (`A2A`)
6. 🟡 **Smart-home A2A.** Home Assistant users asked for A2A in April 2025; no shipped integration surfaced in repeated checks. HA only has its own assistants + Matter. (`A2A`)
7. 🟡 **Healthcare A2A.** No live public agent card found — only think-pieces. Biggest regulated domain in the protocol stack = empty. (`A2A`)
8. 🟡 **Agent Card security scanner.** Card spoofing/poisoning is documented by Semgrep, LevelBlue, KeySight, SecureW2, Palo Alto and arXiv — nobody sells the dominant "curl the card, detect the lie" product. (`MCP`/`LIB`)
9. 🔴 **Certifiable A2A conformance.** Community checkers exist (a2a-check, a2a-inspector) and discussion #1654 published a 23-test suite's findings — but there is no official, certifying suite. (`LIB`)
10. 🔴 **ACP↔A2A bridge beyond coding.** `a2acode` serves ACP *coding* agents over A2A — the generic, non-coding, both-directions bridge is still open. (`LIB`)
11. 🟢 **Absence-native infrastructure.** `ABSENCE-PROTOCOL.md` §16 lists its own future work: absence-native MCP server, burn oracles, reverse compilation, negotiation. None built. (`MCP`/`LIB`)
12. 🟡 **Enterprise agent payment metering.** x402/AP2 activity is crypto-first; per-task metering, invoices, and SLAs for enterprise A2A (thin, auditable, fiat) is open. (`LIB`)

---

## 0b. Built since this research (this repo's own answers)

Gaps from the list below that are now closed by code in this repository — listed here so the
"first of its kind" claims stay checkable against what was built:

| Gap from this doc | Built | Server / agent |
|---|---|---|
| #3 city / municipal A2A | NYC 311 agent card, live on NYC Open Data | `servers/nyc311` (a2a) |
| A — municipal sensor nets | FloodNet street-flooding agent with a flood watch; DEP tap-water agent | `servers/nycflood`, `servers/nycwater` (a2a) |
| A — government feeds with no agent face | NWS alerts, USGS earthquakes, NOAA tides, openFDA recalls, USGS volcano alert levels, NOAA NDBC buoy observations, FAA NAS airport status, Open-Meteo air quality (each previously had no card) | `servers/nws`, `servers/quakes`, `servers/tides`, `servers/recalls`, `servers/volcanoes`, `servers/buoys`, `servers/airports`, `servers/air` (a2a) |
| G — real, unserved niches | Space weather (SWPC Kp + OVATION aurora probability) and wildfire (NIFC WFIGS incidents) | `servers/aurora`, `servers/fire` (a2a) |
| #4 travel / transportation A2A (empty directory section) | Airport status by card: ground-delay programmes, arrival/departure windows and closures from the FAA's own snapshot | `servers/airports` (a2a) |
| #2 non-coding ACP agents | Civic, hazards, Treasury, NHTSA vehicles, plus the ACP→A2A bridge | `acp` repo |
| #10 ACP↔A2A bridge beyond coding | A bridge that also relays A2A push notifications into the editor session | `acp/agents/a2a_bridge` |

Unbuilt gaps that remain open here: the official registry, card security scanning, certifiable
conformance, smart-home, healthcare, travel booking, enterprise payment metering.

---

## 1. Ground truth — what already exists (so "new" means new)

### ACP (Agent Client Protocol) — editor ↔ agent
- Official registry live since **Jan 2026** (Zed + JetBrains). 40+ agents listed, **all coding/engineering**: Claude Agent, Codex, Gemini CLI, Cursor, Copilot, Junie, Amp, Devin, Kimi, Qwen, Mistral Vibe, Snowflake Cortex Code, Stakpak, open-source agents, etc. Only near-exception: Agoragentic (marketplace front-end).
- Clients are broad: editors/IDEs (Zed, JetBrains, VS Code extensions, Neovim, Emacs, Obsidian, Sublime, Qt Creator, Unity), notebooks (Jupyter, marimo), DuckDB, mobile, messaging bridges.
- Depth of client-side coverage greatly exceeds agent-side coverage. The toolbox is wide; what gets put in it is 95% code assistants. **That asymmetry is the ACP gap.**

### A2A (Agent2Agent) — agent ↔ agent
- v1.0 stable (March 2026); moved from the Linux Foundation into the **Agentic AI Foundation (AAIF)** in Aug 2026 — the same home as MCP. AAIF reports 250+ members (Google, Microsoft, Amazon, Anthropic, OpenAI, Bloomberg, Shopify, Block…). 150+ supporting orgs, 22k+ GitHub stars, 5 language SDKs.
- Cloud plumbing done: Azure AI Foundry, Copilot Studio, Amazon Bedrock AgentCore.
- **Production** claimed in: supply chain, financial services, insurance, IT operations. These are internal enterprise deployments — mostly invisible to the public.
- Public live servers (the ones with reachable architecture: agent cards, JSON-RPC) are dominated by crypto/x402 micropayment agents, trust/reputation scoring, dev-tool wrappers, niche data services. Two big thirds of the public economy — **knowledge work and travel** — have empty directory sections.
- Commerce side is moving on other tracks: UCP (Google, Jan 2026; Walmart, Target, Shopify, 20+ partners), AP2 (60+ orgs), OpenAI/Stripe's Agentic Commerce Protocol. Note: that's a **third** thing called ACP — see §5.

### Directories / registries / marketplaces
- **ACP:** official registry JSON (`cdn.agentclientprotocol.com/registry/v1/latest/registry.json`), GitHub repo, JetBrains/Zed discovery.
- **A2A:** no official registry yet (LF roadmap: registry consolidation upcoming). Community: a2a-registry.org, sing1ee/a2a-directory, pab1it0/awesome-a2a, proposal discussion #924.
- **Enterprise marketplaces:** Salesforce AgentExchange (~14k listings), Microsoft Marketplace + Microsoft 365 agent registry, AWS Agent Registry (Aug 2026), Google. These are product catalogs, not protocol-native discovery.

---

## 2. The giant list

### A — Cities & public sector (things no city agency has published)

| # | Gap | Closest existing | Status | Shape |
|---|---|---|---|---|
| A1 | City 311 A2A agent (report issue, check status) | Boston open-data MCP agents; Tars 311 chatbots (closed) | 🟢 | A2A |
| A2 | Municipal open-data A2A server any agent can query | Boston MCP agents (MCP only) | 🟢 | A2A |
| A3 | Permit / zoning A2A server | Salesforce city-services agents (closed product) | 🟡 | A2A |
| A4 | City procurement / RFP / bid agent | None found | 🟡 | A2A |
| A5 | Transit schedule + disruption A2A (GTFS front door) | GTFS feeds, transit apps (no agent interface) | 🟡 | A2A |
| A6 | Public library catalog / holds / card-signup agent | Library apps; no agent cards | 🟡 | A2A |
| A7 | DMV / license renewal agent | Proprietary gov portals | 🟡 | A2A |
| A8 | Court dates / jury duty / fine payment agent | Gov portals only | 🟡 | A2A |
| A9 | Water + utility outage agent (city-owned) | Utility web maps | 🟡 | A2A |
| A10 | Parking ticket dispute agent | Vendor portals | 🟡 | A2A |
| A11 | School district enrollment / bus route agent | K-12 admin agent vendors (closed) | 🟡 | A2A |
| A12 | Emergency alert / shelter lookup agent | Alert feeds, no protocol face | 🟡 | A2A |
| A13 | City council meeting digest agent | Civic tech newsletters | 🟡 | A2A |
| A14 | Parks / pool / rec reservation agent | Booking portals | 🟡 | A2A |
| A15 | "One agent card per city" (delegating to departments) | Nothing | 🟢 | A2A |
| A16 | Federal agency agent card on a .gov domain | GSA MCP-agent hackathon (Sept–Nov 2026, MCP only) | 🟡 | A2A/MCP |
| A17 | State benefits / unemployment status agent | State portals | 🟡 | A2A |
| A18 | Public housing maintenance request agent | Housing authority portals | 🟡 | A2A |
| A19 | Polling place / election info agent | Election office websites | 🟡 | A2A |
| A20 | Multilingual (ES/ZH/…) civic A2A with translation built into the task lifecycle | Nothing found | 🟡 | A2A |

### B — Companies & industries (whole verticals with no public agent card)

| # | Gap | Closest existing | Status | Shape |
|---|---|---|---|---|
| B1 | Airline booking-management A2A server | Nothing live; article "Why Can't Your AI Agent Book a Flight?"; ServiceNow lab demo | 🟢 | A2A |
| B2 | Hotel inventory / booking A2A | OTA chat assistants | 🟡 | A2A |
| B3 | Bank account agent (balance, disputes, transfers w/ consent) | Bank chatbots; no agent cards | 🟡 | A2A |
| B4 | Insurance claim filing + status A2A | LF lists insurance as in-production (internal) | 🔴 | A2A |
| B5 | Hospital / clinic scheduling A2A | None public | 🟡 | A2A |
| B6 | Pharmacy refill + interaction check A2A | Pharmacy apps | 🟡 | A2A |
| B7 | University public A2A (courses, credits, advising) | Vendor enrollment agents (Druid, Element451 — closed) | 🟢 | A2A |
| B8 | Law-firm intake / matter status A2A | Ambr (contracts, crypto payments) — adjacent only | 🔴 | A2A |
| B9 | Accounting / bookkeeping close agent for SMBs | SaaS apps | 🟡 | A2A |
| B10 | Mortgage / title status A2A | Lender portals | 🟡 | A2A |
| B11 | Construction estimate audit A2A | HORIZON SHIELD (Japan, live, niche) | 🔴 | A2A |
| B12 | Farm / agronomy advisory A2A (weather + soil + equipment) | Agtech apps | 🟡 | A2A |
| B13 | Grid / energy retailer agent (usage, outage, tariff switch) | Utility portals | 🟡 | A2A |
| B14 | Solar installer quote + monitoring agent | Solar apps | 🟡 | A2A |
| B15 | Manufacturing supply-chain exception agent (public) | LF: supply chain in production (internal) | 🔴 | A2A |
| B16 | Trucking load board A2A (book a load) | Load boards | 🟡 | A2A |
| B17 | Restaurant reservation + waitlist A2A | Reservation apps | 🟡 | A2A |
| B18 | Grocery store inventory + substitution A2A (small chains) | Big-retail agentic commerce (UCP partners) | 🔴 | A2A |
| B19 | Retail loyalty point redemption across brands | Loyalty apps | 🟡 | A2A |
| B20 | Newsroom "ask the archive" rights-managed A2A | News sites | 🟡 | A2A |
| B21 | Music licensing / sync rights A2A | None found | 🟡 | A2A |
| B22 | Game studio live-ops A2A (events, economy) | Game APIs | 🟡 | A2A |
| B23 | Sports team ticketing + schedule A2A | Ticket vendors' assistants | 🟡 | A2A |
| B24 | Museum / gallery collection A2A (rights-aware) | Museum APIs | 🟡 | A2A |
| B25 | Veterinary records / appointment A2A | Vet portals | 🟡 | A2A |
| B26 | Childcare / camp enrollment A2A | Paper + portals | 🟡 | A2A |
| B27 | Elder-care coordination A2A | Care apps | 🟡 | A2A |
| B28 | Nonprofit donation + impact report A2A | Donation widgets | 🟡 | A2A |
| B29 | B2B wholesale quote / MOQ agent (SMB manufacturers) | EDI, email | 🟡 | A2A |
| B30 | Freight/customs broker agent (documents, duties) | Broker portals | 🟡 | A2A |

### C — Consumer & home

| # | Gap | Closest existing | Status | Shape |
|---|---|---|---|---|
| C1 | Home Assistant A2A integration | Feature request thread (Apr 2025); HA assistants are internal | 🟡 | A2A |
| C2 | Matter/thread device control exposed as an agent, per-household auth | HA + Matter (no protocol face) | 🟡 | A2A |
| C3 | Personal finance agent (bank + card + bills, consent-scoped) | Finance apps | 🟡 | A2A |
| C4 | Family calendar negotiation agent (find a slot that works) | Calendar APIs | 🟡 | A2A |
| C5 | Cross-store grocery basket optimizer | Single-store apps | 🟡 | MCP |
| C6 | Fridge/pantry inventory → meal → order loop | Recipe apps | 🟡 | A2A |
| C7 | Wardrobe / laundry agent | Closet apps | 🟡 | A2A |
| C8 | Car service + recall + fuel agent (per-VIN) | Brand apps | 🟡 | A2A |
| C9 | Door-to-door family trip agent (whole itinerary, all vendors) | Trip planners (no A2A) | 🟡 | A2A |
| C10 | Pet tracker + vet + meds agent | Pet apps | 🟡 | A2A |
| C11 | Plant care agent (species + local weather) | Plant apps | 🟡 | A2A |
| C12 | Printer / scanner local agent (paperwork inbox) | Vendor clouds | 🟡 | MCP |
| C13 | Home energy optimizer (tariff + battery + EV) | HEMS products | 🟡 | A2A |
| C14 | Robot vacuum + mop fleet orchestration | Vendor apps | 🟡 | A2A |
| C15 | 3D printer queue agent (slice → print → notify) | OctoPrint et al. | 🟡 | MCP |
| C16 | Smart TV / media agent with per-profile rules | TV OS assistants | 🟡 | MCP |

### D — Hardware, robotics, IoT

| # | Gap | Closest existing | Status | Shape |
|---|---|---|---|---|
| D1 | ROS 2 ↔ A2A bridge (robot as an agent) | MCP bridges exist for some robotics; not A2A | 🟡 | LIB |
| D2 | Drone fleet task delegation A2A (survey, inspect) | Vendor SDKs | 🟡 | A2A |
| D3 | Warehouse robot ↔ WMS agent boundary | Vendor lock-in | 🟡 | A2A |
| D4 | CNC / laser / shop machine agent (job status) | Vendor clouds | 🟡 | MCP |
| D5 | Farm equipment field ops agent | Ag OEM clouds | 🟡 | A2A |
| D6 | Lab instrument agent (protocol, sample queue) | Opentrons protocols, MCP tools | 🟡 | MCP |
| D7 | Telescope / observatory scheduling agent | Observatory software | 🟡 | A2A |
| D8 | Weather station mesh agent | Weather APIs | 🟡 | A2A |
| D9 | Camera / NVR event agent with consent rules | NVR apps | 🟡 | MCP |
| D10 | Embedded `📟`-class A2A (microcontroller as task server) | A2A has no embedded story published | 🟡 | LIB |

### E — Developer & protocol infrastructure

| # | Gap | Closest existing | Status | Shape |
|---|---|---|---|---|
| E1 | Official A2A registry with signed-card verification | Community only: a2a-registry.org, a2a-directory, discussion #924; LF roadmap says consolidation upcoming | 🟢 | LIB |
| E2 | Generic ACP↔A2A bridge (non-coding, bidirectional) | a2acode (coding agents over A2A only) | 🔴 | LIB |
| E3 | Open-source MCP↔A2A reference gateway | Gatana (commercial), MuleSoft Flex Gateway | 🔴 | LIB |
| E4 | Agent Card security scanner (spoof/poison/typosquat detection) | Semgrep/LevelBlue/KeySight writeups; no dominant product | 🟡 | MCP |
| E5 | Enterprise agent identity + reputation (non-crypto) | On-chain: AgentRank, TWZRD, SwarmScore; wallet-gated | 🔴 | LIB |
| E6 | Certifiable A2A conformance suite | a2a-check CLI, a2a-inspector; findings #1654; no official suite | 🔴 | LIB |
| E7 | A2A-native observability (span conventions, task traces) | Generic LLM observability (Langfuse, etc.), Grafana how-tos, gateway logs | 🔴 | LIB |
| E8 | ACP Registry category expansion: non-dev agents | Registry is coding-only | 🟢 | ACP |
| E9 | A2A uptime/downtime monitor for public agents | General status pages | 🟡 | MCP |
| E10 | Agent card change-diff alerts (capability drift) | Nothing found | 🟡 | MCP |
| E11 | Task replay debugger for failed A2A runs | a2a-inspector (manual inspection) | 🔴 | LIB |
| E12 | Cross-protocol conformance runner (MCP+ACP+A2A in one) | Nothing found | 🟡 | LIB |
| E13 | Registry poisoning / sybil defense for public cards | Crypto trust-scoring only | 🟡 | LIB |
| E14 | Cost + SLA metering for delegated tasks (fiat, enterprise) | x402 (crypto), AP2 (payments, not metering) | 🟡 | LIB |
| E15 | ACP agent permission audit ("what did it touch in my repo?") | DIFF UI in clients | 🔴 | MCP |
| E16 | One-click "wrap my API as an A2A server" (no SDK) | Bindu, ADK, LangGraph adapters (dev-heavy) | 🔴 | LIB |
| E17 | Agent card → testable mock server generator | agentcard.net generator (card only) | 🔴 | MCP |
| E18 | A2A in CI: smoke-suite for agent PRs | Community a2a-check | 🔴 | SKILL |
| E19 | Directory of ACP clients by niche (market map) | Website list (flat) | 🔴 | SKILL |
| E20 | Local-first A2A server behind NAT with identity hosting | clayborn (early, personal-agent niche) | 🔴 | LIB |

### F — Absence / meta (from `ABSENCE-PROTOCOL.md`'s own future-work list)

| # | Gap | Closest existing | Status | Shape |
|---|---|---|---|---|
| F1 | `MISSING.md` parser + reference `absence` CLI | Spec only, §10.2 | 🟢 | LIB |
| F2 | Absence-native MCP server (`absence.note/ls/compile`) | Spec future work §16 | 🟢 | MCP |
| F3 | `acp-agent` compiler target (emit ACP agent scaffold + failing test) | Spec `compiles_to`; no compiler | 🟢 | ACP |
| F4 | Burn oracles (independent wasted-compute verification) | Spec future work §16 | 🟢 | LIB |
| F5 | Absence negotiation between two agents | Spec future work §16 | 🟢 | LIB |
| F6 | Reverse compilation (infer gaps closed by an existing MCP server) | Spec future work §16 | 🟢 | LIB |
| F7 | Gap dedupe by embedding + heuristic | Spec `absence dedupe`; unbuilt | 🟢 | LIB |
| F8 | Federated `/.well-known/absence.json` index | Spec §12.2; unbuilt | 🟢 | LIB |
| F9 | Stale-gap regression watcher (falsifier fires → re-open) | Spec lifecycle; unbuilt | 🟢 | LIB |
| F10 | Gap → marketplace: turn open gaps into paid bounties (observable build demand) | Issue trackers; no burn-weighted bounty board | 🟡 | A2A |

### G — Weird & novel (real, unserved niches)

| # | Gap | Closest existing | Status | Shape |
|---|---|---|---|---|
| G1 | Cemetery / grave-location + records agent | Find-a-grave sites | 🟡 | A2A |
| G2 | House of worship bulletin / service-times agent | Church websites | 🟡 | A2A |
| G3 | Prayer-times + community calendar agent (mosque) | Prayer apps | 🟡 | A2A |
| G4 | Street food truck tracker agent | Instagram accounts | 🟡 | A2A |
| G5 | Fishing conditions + bag-limit checker agent | Angler forums | 🟡 | A2A |
| G6 | Surf report agent with spot-level auth | Surf apps | 🟡 | A2A |
| G7 | Mushroom/foraging safety agent (region-aware rules) | ID apps | 🟡 | A2A |
| G8 | Birdwatching rarity alert agent | eBird alerts | 🟡 | A2A |
| G9 | Homebrew recipe + yeast inventory agent | Recipe sites | 🟡 | A2A |
| G10 | Tool-lending library agent (loans, holds) | Library systems | 🟡 | A2A |
| G11 | Community fridge inventory agent | Volunteer spreadsheets | 🟡 | A2A |
| G12 | Seed-library / plant-swap agent | Garden clubs | 🟡 | A2A |
| G13 | Chess club pairing + rating agent | Club software | 🟡 | A2A |
| G14 | Dog-park conditions + social agent | Group chats | 🟡 | A2A |
| G15 | Local civic-event (council, protest, festival) agent | Facebook events | 🟡 | A2A |
| G16 | TTRPG table runner as an A2A service | VTT apps | 🟡 | A2A |
| G17 | Time-capsule / anniversary agent with long-horizon tasks | Reminder apps | 🟡 | A2A |
| G18 | Star/ISS pass + astronomy-night agent | Astronomy apps | 🟡 | A2A |

---

## 3. Strongest verified absences (deep-checked)

These got the most scrutiny because they're the least contestable:

1. **Official A2A registry** — LF's own April 2026 one-year release lists "consolidation of efforts for registry and expanded testing and tooling" as *roadmap*. Secondary attempts exist but none is official.
2. **Non-coding ACP agents** — the full ACP registry JSON was pulled and read. Every entry is a coding/engineering agent; one marketplace wrapper. Zero domain agents (finance, science, design, data, health).
3. **Public city A2A** — searched municipal + govtech + GSA material: Boston is MCP, GSA hackathon is MCP, Salesforce city agents are closed SaaS. No city publishes `/.well-known/agent-card.json`.
4. **Travel A2A** — awesome-a2a's Travel & Transportation section: "No entries yet." Independent 2026 coverage asks "Why can't your AI agent book a flight?" — i.e., press confirms the gap.
5. **Knowledge services A2A** — awesome-a2a's Knowledge Services section: "No entries yet."
6. **Smart-home A2A** — Home Assistant community request (Apr 2025) remains a request; repeated searches surfaced no shipped integration.
7. **Absence-native tooling** — `ABSENCE-PROTOCOL.md`'s own §16 calls these future work. Nothing on the registry/directories resembles them.

## 4. Double-check, and keep double-checking

Absence has a shelf life. Re-run these before building anything from this list:

```bash
# ACP: full official agent list (check for a domain agent before claiming the gap)
curl -s https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json

# A2A: is a candidate live? (documented discovery path)
curl -s https://<candidate-domain>/.well-known/agent-card.json

# Directories to eyeball
#   https://github.com/pab1it0/awesome-a2a
#   https://github.com/sing1ee/a2a-directory
#   https://www.a2a-registry.org/
```

Only a live, fetchable Agent Card (or a registry entry) counts as "taken". A blog post announcing intent does not.

## 5. Caveats — what this list does not claim

- **Absence can't be proven for private systems.** A bank running A2A internally and not telling anyone is indistinguishable from a bank with nothing. The 🟡 rows are "no public evidence", not "does not exist".
- **The word ACP now has three meanings** — Agent Client Protocol (editors), Agent Communication Protocol (IBM, merged into A2A), and Agentic Commerce Protocol (OpenAI/Stripe). Secondary sources mix them up constantly; at least one 2026 blog even invents an "Amazon ACP". Verify every source's definition before citing it.
- **Directories lag reality.** A2A's public-agent directories are community-maintained; a live server may be missing from them. Treat directory empties as a strong hint, not proof.
- **This list is a snapshot of 2026-09-22.** The A2A ecosystem moves weekly; re-verify before investment.

## 6. Sources (consulted 2026-09-22)

Primary pages for §1–§3 were opened and read directly; the row-level links below were located via search and used at snippet level. Re-verify before building (see §4).

- ACP intro & registry — https://agentclientprotocol.com/get-started/introduction · /get-started/registry · /get-started/agents · /get-started/clients
- ACP registry feed — https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json
- A2A one-year release (150+ orgs, v1.0, roadmap) — https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year
- A2A → Agentic AI Foundation (Aug 2026) — https://www.axios.com/2026/08/17/a2a-agentic-ai-foundation-open-ai-standards · https://aaif.io/blog/a2a-joins-aaif
- A2A discovery + registry discussion — https://a2a-protocol.org/latest/topics/agent-discovery/ · https://github.com/a2aproject/A2A/discussions/924
- A2A conformance findings — https://github.com/a2aproject/A2A/discussions/1654 · https://github.com/a2aproject/A2A/issues/1376
- awesome-a2a (empty Travel + Knowledge sections) — https://github.com/pab1it0/awesome-a2a
- A2A directory — https://github.com/sing1ee/a2a-directory · https://www.a2a-registry.org/
- ACP→A2A bridge (`a2acode`) — https://github.com/kanywst/a2acode
- Agent-card security — https://semgrep.dev/blog/2025/a-security-engineers-guide-to-the-a2a-protocol · https://www.levelblue.com/blogs/spiderlabs-blog/agent-in-the-middle-abusing-agent-cards-in-the-agent-2-agent-protocol-to-win-all-the-tasks · https://www.keysight.com/blogs/en/tech/nwvs/2026/03/12/agent-card-poisoning · https://arxiv.org/html/2602.11327v1
- Cities: Boston MCP agents — https://medium.com/ogp-horizons/the-machine-legible-record-in-an-age-of-ai-agents-government-open-data-needs-an-update-35459cec9169 · GSA MCP hackathon — https://www.gsa.gov/artificial-intelligence/ai-community-of-practice/events-and-training/mcp-server-and-ai-agent-government-hackathon
- Home Assistant A2A request — https://community.home-assistant.io/t/a2a-agent2agent-protocol-integration-and-support-new-open-protocol-enabling-communication-and-interoperability-between-agentic-ai-applications-that-is-complementary-to-the-mcp-protocol/877673
- Travel gap — https://aleximas.substack.com/p/why-cant-your-ai-agent-book-a-flight · https://www.oag.com/blog/march-2026-the-month-agentic-travel-gets-real
- Agentic commerce (UCP/AP2, and the third "ACP" = Agentic Commerce Protocol) — https://blog.google/products/ads-commerce/agentic-commerce-ai-tools-protocol-retailers-platforms/ · https://cloud.google.com/blog/products/ai-machine-learning/announcing-agents-to-payments-ap2-protocol · https://eco.com/support/en/articles/14839400-what-is-agentic-commerce-the-2026-guide
- Enterprise marketplaces — https://www.salesforce.com/agentforce/agentexchange/ · https://aws.amazon.com/blogs/machine-learning/manage-agents-tools-and-skills-at-scale-with-aws-agent-registry/
- A2A reality check (observability, marketplaces, security) — https://www.glukhov.org/ai-systems/comparisons/a2a-protocol-2026-adoption/
- IBM's BeeAI ACP (communication protocol, merged into A2A) — https://www.ibm.com/think/topics/agent-communication-protocol

---

## 7. The four newest servers — gap checks (2026-09-23)

Checked the same way as §3: registries and directories first, then a targeted search for the closest
existing thing. Per §4, only a live, fetchable Agent Card counts as *taken*; adjacent **MCP** servers
and plain data APIs are named because that is all the gap consists of.

| Claim | Closest existing thing found | Maps to | Verdict |
|---|---|---|---|
| First air-quality A2A server | Open-Meteo, CAMS and AirNow publish the data, and MCP servers and AQI apps wrap it; no agent card found | new (atmospheric / environmental feeds) | 🟡 no public card found |
| First volcano alert-level A2A server | USGS's Volcano Notification Service emails subscribers and the VSC API is public, but nothing protocol-native; AVO and GVP publish pages only | A16 (federal agency card) | 🟡 no public card found |
| First marine-buoy A2A server | `cyanheads/noaa-marine-mcp-server` (**MCP**, covers NDBC buoys and tides), a Home Assistant NDBC integration, and an apis.io catalogue entry for the API itself | A (oceanographic feeds with no agent face) | 🟡 no public card found |
| First airport-status A2A server | Flight-tracking MCP servers, airline apps and the FAA's own status page; no live card | #4 travel & transportation (empty category) · B1 airline booking | 🟡 no public card found |

Why 🟡 and not 🟢: these four are each a single agency feed, not a whole vertical, so they get the
weaker mark even though nothing surfaced on the day. Read every "first" in `README.md` as "no public
agent card found on 2026-09-23" — and per §5, a private deployment would be invisible from here.
