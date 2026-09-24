# NYC Open Data → A2A Skills — verified catalog

**Verified:** 2026-09-22, against the live Socrata APIs (not blog summaries). §9 covers the
non-NYC datasets used by the later servers in this repo.
**Endpoint pattern:** `https://data.cityofnewyork.us/resource/<id>.json` — public, read-only, no key needed for light use (an app token raises rate limits).
**How each row was checked:** Socrata Discovery API for existence + freshness, then a live `$limit=1` probe for real field names, then `count(*)` where a row count is shown. Official dataset names pulled from `https://data.cityofnewyork.us/api/views/<id>.json`.

Reproduce any row:

```bash
curl -s "https://data.cityofnewyork.us/resource/<id>.json?\$limit=1"     # real fields
curl -s "https://data.cityofnewyork.us/resource/<id>.json?\$select=count(*)"  # scale
```

---

## 1. Probe-verified datasets (fields read from real rows)

### 311 & civic core
| ID | Dataset | Scale | Updated | Fields you'd use | Skill idea | Watch? |
|---|---|---|---|---|---|---|
| `erm2-nwe9` | 311 Service Requests 2020–present | 22,550,280 | 2026-09-22 | unique_key, created_date, closed_date, complaint_type, descriptor, status, agency, borough, incident_zip, street_name, resolution_description | Complaint status; complaints near a block/zip; "is this a known problem on my street" | ✅ status changes |

### Streets & getting around
| ID | Dataset | Scale | Updated | Fields you'd use | Skill idea | Watch? |
|---|---|---|---|---|---|---|
| `xnfm-u3k5` | Street Resurfacing Schedule | — | 2026-09-21 | boroughname, day, date, onstreetname, fromstreetname, tostreetname, worktype, crewtype | "Is my street getting repaved this week?" | ✅ schedule drops weekly |
| `tqtj-sjs8` | Street Construction Permits (2022–present) | — | 2026-09-21 | permitnumber, permitstatusid/shortdesc, permittypedesc, permitissuedate, issuedworkstartdate/enddate, boroughname, onstreetname, fromstreetname | "Why is my street dug up, how long will it last?" | ✅ new permits |
| `uiay-nctu` | Open Streets Locations | — | 2026-09-01 | (locations + hours) | "Which streets are car-free right now?" | ✅ |
| `mzxg-pwib` | NYC Bike Routes | — | 2026-07-24 | boro, street, fromstreet, tostreet, facilitycl, bikedir, lanecount | "Route me on protected lanes" | ❌ static |
| `ez4e-fazm` | Bus Breakdown and Delays | 1,297,640 | 2026-09-21 | school_year, bus_no, route_number, reason, occurred_on, boro, bus_company_name, breakdown_or_running_late | "Is my kid's school bus late?" (school buses — DOE vendors, not MTA) | ✅ daily |
| `t5n6-gx8c` | NYC Ferry Ridership | — | 2026-09-17 | date, hour, route, direction, stop, boardings, typeday | "How busy is my ferry, best time to ride" | ❌ analytic |
| `693u-uax6` | Parking Meters Locations and Status | — | 2026-09-21 | meter_number, status, meter_hours, on_street, from_street, to_street, lat, long | "Is there a working meter on this block?" | ✅ status |

### Safety & enforcement
| ID | Dataset | Scale | Updated | Fields you'd use | Skill idea | Watch? |
|---|---|---|---|---|---|---|
| `nc67-uf89` | Open Parking and Camera Violations | 151,303,530 | 2026-09-20 | plate, state, summons_number, issue_date, violation, fine_amount, penalty_amount, amount_due, precinct, county, issuing_agency | **"Do I have tickets? What do I owe?" by plate** | ✅ new summons |
| `jz4z-kudi` | OATH Hearings Division Case Status | — | 2026-09-22 | ticket_number, violation_date, issuing_agency, violation_location_zip_code, hearing_result, hearing_date, decision_date, penalty_imposed | "What happened at my ticket hearing?" | ✅ decisions |
| `h9gi-nx95` | Motor Vehicle Collisions — Crashes | 2,269,187 | ⚠️ paused | crash_date, crash_time, on_street_name, off_street_name, persons_injured/killed, pedestrians/cyclists/motorists, contributing_factor_vehicle_1, vehicle_type_code1, collision_id | "Crash history at this corner" | ⚠️ see caveats |
| `8m42-w767` | Fire Incident Dispatch Data | — | 2026-07-09 | incident_datetime, incident_borough, zipcode, alarm_source_description_tx, incident_classification, dispatch_response_seconds_qy | "What was that siren / fire on my block?" | ✅ |
| `ii3r-svjz` | Bureau of Fire Investigations — Fire Causes | — | 2026-09-14 | (cause + origin fields) | "Why do fires start here — prevention view" | ❌ |
| `uip8-fykc` | NYPD Arrest Data YTD | 141,870 | 2026-07-27 | arrest_date, ofns_desc, law_cat_cd, arrest_boro, arrest_precinct, age_group, perp_sex, perp_race | "Arrests near me, by offense type" (handle with care) | ✅ |
| `5uac-w243` | NYPD Complaint Data Current YTD | — | 2026-07-27 | cmplnt_fr_dt, ky_cd, ofns_desc, boro_nm, prem_typ_desc | "What gets reported on my block" | ✅ |

### Home & housing
| ID | Dataset | Scale | Updated | Fields you'd use | Skill idea | Watch? |
|---|---|---|---|---|---|---|
| `wvxf-dwi5` | Housing Maintenance Code Violations | 11,254,675 | 2026-09-21 | violationid, boro, housenumber, streetname, zip, apartment, class, inspectiondate, originalcorrectbydate, violation status | **"Does my building have open violations?"** (by address) | ✅ new/closed violations |
| `6z8x-wfk4` | Evictions | 134,049 | 2026-09-21 | court_index_number, docket_number, eviction_address, executed_date, marshal names, borough, eviction_zip, residential_commercial_ind | "Eviction activity on my block" | ✅ |
| `59kj-x8nc` | Housing Litigations | — | 2026-09-01 | litigationid, housenumber, streetname, zip, casetype, caseopendate, casestatus, casejudgement, respondent | "Is my landlord in housing court?" | ✅ |
| `hg8x-zxpr` | Affordable Housing Production by Building | — | 2026-05-19 | (project, units, borough, completion) | "New affordable units near me" | ❌ |

### Food, pests, health
| ID | Dataset | Scale | Updated | Fields you'd use | Skill idea | Watch? |
|---|---|---|---|---|---|---|
| `43nn-pn8j` | Restaurant Inspection Results | 295,295 | 2026-09-21 | camis, dba, boro, building, street, zipcode, cuisine_description, inspection_date, action, violation_code, critical_flag, score, grade | **"Is this restaurant clean? What's its grade?"** | ✅ grade changes |
| `p937-wjvj` | Rodent Inspection | 3,125,781 | 2026-09-20 | job_id, inspection_date, inspection_type, zip_code, borough, result, block, lot | **"Rat activity near me"**; "did my building get inspected?" | ✅ results |
| `5uug-f49n` | Harbor Water Quality | — | 2026-09-15 | sampling_location, sample_date, weather_condition_dry_or_wet, top/bottom temp, salinity, dissolved oxygen, % O2 saturation, pH/chlorophyll (profile fields) | "Can I swim at this beach today?" | ✅ samples |
| `c3uy-2p5r` | Air Quality and Health Impacts | — | 2026-06-18 | (PM2.5, health impact estimates) | "Air quality + health impact for my area" | ❌ |
| `y43c-5n92` | Watershed Water Quality Data | — | 2026-09-07 | (reservoir sampling) | "Upstate water quality" | ✅ |

### Buildings & permits
| ID | Dataset | Scale | Updated | Fields you'd use | Skill idea | Watch? |
|---|---|---|---|---|---|---|
| `ipu4-2q9a` | DOB Permit Issuance | — | 2026-09-21 | (permit details, address) | "What's being built next door?" | ✅ |
| `rbx6-tga4` | DOB NOW: Build — Approved Permits | — | 2026-09-21 | (approved permits) | same, newer system | ✅ |
| `ic3t-wcy2` | DOB Job Application Filings | — | 2026-09-21 | (job filings) | "Is that building about to be demolished?" | ✅ |
| `tg4x-b46p` | Film Permits | — | 2026-09-21 | eventid, eventtype, startdatetime, enddatetime, eventagency, parkingheld, borough, communityboard_s, policeprecinct_s, category, subcategoryname, zipcode_s | **"What's filming in my neighborhood this week?"** | ✅ new permits |
| `w7w3-xahh` | Issued Licenses | — | 2026-09-18 | (license type, address) | "Who's licensed to operate on my block" | ✅ |

### Parks & green
| ID | Dataset | Scale | Updated | Fields you'd use | Skill idea | Watch? |
|---|---|---|---|---|---|---|
| `hn5i-inap` | Forestry Tree Points | — | 2026-09-09 | globalid, genusspecies, dbh, tpstructure, tpcondition, plantingspaceglobalid, geometry | "What tree is outside my door? Who maintains it?" | ✅ |
| `uvpi-gqnh` | 2015 Street Tree Census | — | 2024-11-12 | tree_id, tree_dbh, status, health, spc_latin, spc_common, problems, sidewalk | Species/health baseline (historical, 2015) | ❌ |
| `enfh-gkve` | Parks Properties | — | 2026-09-16 | (park polygons, names) | "Parks near me" | ❌ |

### Civic & jobs
| ID | Dataset | Scale | Updated | Fields you'd use | Skill idea | Watch? |
|---|---|---|---|---|---|---|
| `dg92-zbpx` | City Record Online | 1,105,564 | 2026-09-16 | request_id, start_date, end_date, agency_name, short_title, section_name, additional_description_1 | **"What is the city proposing/buying/hearing in my district?"** | ✅ daily notices |
| `kpav-sd4t` | Jobs NYC Postings | 2,711 | 2026-09-15 | business_title, civil_service_title, job_category, salary_range_from/to, work_location, job_description, minimum_qual_requirements | "City jobs matching me" | ✅ |
| `vx8i-nprf` | Civil Service List (Active) | — | 2026-09-21 | (list + status) | "Am I on the list?" | ✅ |
| `rc75-m7u3` | COVID-19 Daily Counts | — | 2026-09-21 | (cases, hospitalizations, deaths) | "Current COVID levels" | ✅ |
| `qgea-i56i` | NYPD Complaint Data Historic | — | 2026-04-28 | cmplnt_num, cmplnt_fr_dt, ofns_desc, law_cat_cd, boro_nm, prem_typ_desc | Long-run trends | ❌ |

### Fun (real, even if niche)
| ID | Dataset | Scale | Updated | Skill idea |
|---|---|---|---|---|
| `vfnx-vebw` | 2018 Central Park Squirrel Census | — | 2023-04-18 | "Squirrel census facts" (stale, delightful) |
| `4y8i-pbvd` | Subway Rider Survey (2019) | — | 2025-12-11 | Historical rider sentiment |

---

## 2. Also in the catalog (metadata checked, fields not yet probed)

From the same Discovery API sweep — verified to exist and fresh, ready to probe when a skill needs them:

- `xnfm-u3k5`-adjacent streets: `uiay-nctu` Open Streets, `p4u2-3jgx` Sidewalk Dismissal, `qt6m-xctn` Street Sign Work Orders, `bheb-sjfi` Sidewalk Correspondences
- Housing: `im9z-53hg` NYCHA violations, `9rz4-mjek` Tax Lien Sale Lists, `3h2n-5cm9` DOB Violations, `eabe-havv` DOB Complaints Received
- TLC: `8wbx-tsch` FHV Active vehicles, `xjfq-wh2d` FHV Active drivers, `dpec-ucu7` TLC New Driver Application Status
- Schools: `dnpx-dfnc` School Quality Reports, `pc34-d3sx` Safe Routes Priority Schools
- Water: `hphy-6g7m` Water and Sewer Permits, `ia2d-e54m` Water Consumption
- Weather/flood: `hxm3-23vy` E-Designations (flood risk)

---

## 3. What can't be a skill (honest limits)

- **Filing a 311 complaint** — NYC publishes the results, not an open "submit complaint" API. A real server can *read* status and *link* to the official form, but must not pretend it filed anything.
- **Paying a ticket** — payment lives behind the official portal (CityPay). Read-only lookups only.
- **Real-time subway arrivals** — those live at MTA (different agency + keyed feeds), not NYC Open Data.
- **Anything personal** — these datasets are public records. Skills return public data, never guesses about individuals.

## 4. Rules for skills built on these

1. **Read-only, cite the dataset** — every answer names its dataset ID ("source: NYC Open Data erm2-nwe9").
2. **Cache + backoff** — brief cache per query, honor 429s, optional app token via env for higher limits.
3. **Strings stay strings** — zip/bbl/phone carry leading zeros; keep them strings, never coerce.
4. **Yes/No fields are strings** in several datasets — normalize explicitly.
5. **Freshness check** — each skill reports the dataset's `rowsUpdatedAt` so callers know how stale the answer is (see crashes caveat below).
6. **No PII derivation** — return what the dataset says; never join names to addresses to profiles.

## 5. Caveats found during verification

- **`h9gi-nx95` (Crashes) is paused.** Its own description says the automated update is broken; rows last updated 2026-06-15. Skill can ship but must show "data frozen since June" instead of pretending it's current.
- **`ez4e-fazm` is school buses,** not MTA — does not cover subway/bus commutes.
- **`tg4x-b46p` (Film Permits)** schema uses `event*` field names; it documents street-use/filming events, which is why `parkingheld` appears.
- **Update cadence varies wildly:** 311/permits daily; affordable housing quarterly; census/survey datasets frozen by design.
- **Rate limits** apply without an app token — the server caches and batches.

## 6. Recommended skill roadmap (for the NYC A2A server)

| Version | Skills | Why |
|---|---|---|
| **v1 (MVP, now)** | 311 complaint status · complaints near a zip | Already scoped, daily-fresh, the classic |
| **v1.1 (consumer wins)** | Parking tickets by plate (`nc67-uf89`) · Building violations by address (`wvxf-dwi5`) · Restaurant grade (`43nn-pn8j`) | Highest everyday value, huge datasets, clean lookups |
| **v1.2 (NYC flavor)** | Rats nearby (`p937-wjvj`) · Filming this week (`tg4x-b46p`) · Street repaving (`xnfm-u3k5`) | Distinctly New York, great demos |
| **v1.3 (live-ish, new finds)** | Street flooding right now (`aq7i-eu5q`) · Tap water test results (`bkwf-xfky`) · Bike parking near me (`592z-n7dk`) | Sensor/facility data re-checked 2026-09-22, see §8 |
| **v2 (the ping)** | Webhook watches on: 311 status, restaurant grade, resurfacing schedule, new film permits in a zip, building violations | This is where A2A push notifications shine |
| **v3 (civic)** | City Record notices (`dg92-zbpx`) · City jobs (`kpav-sd4t`) · OATH hearing results (`jz4z-kudi`) | The "informed citizen" layer |

## 7. How a dataset becomes an A2A card skill

One skill = one entry in the card + one handler:

```json
{
  "id": "complaint-status",
  "name": "311 complaint status",
  "description": "Look up a NYC 311 complaint by its unique key and report status, dates, and resolution.",
  "tags": ["nyc", "311", "civic"],
  "examples": ["Did complaint 12345678 get fixed?"],
  "inputModes": ["text/plain", "application/json"],
  "outputModes": ["application/json", "text/plain"]
}
```

The handler queries the dataset, normalizes the fields, returns an artifact, and stamps the dataset ID + `rowsUpdatedAt`. Watch-capable skills also declare their watch keys (e.g., `unique_key` for 311) so the server knows what to poll for push notifications.

---

## 8. Re-check 2026-09-22 — roadmap datasets re-probed + new candidates found

Re-run of the §1 method against every v1.1/v1.2 candidate (live `$limit=1` row + `rowsUpdatedAt`). All 16 answered with the same fields as before, so the roadmap is still valid. Fresh finds from a Socrata catalog sweep are below.

### 8a. Roadmap datasets, re-verified today

| ID | Dataset | Rows | Fields that matter | Watch? |
|---|---|---|---|---|
| `nc67-uf89` | Open Parking and Camera Violations | 151M | plate, state, license_type, summons_number, summons_image, issue_date, violation, violation_time, fine/penalty/interest/amount_due | ✅ new summons |
| `jz4z-kudi` | OATH Hearings Case Status | — | ticket/charge codes + descriptions, hearing_date, decision_date, hearing_result | ✅ decisions |
| `wvxf-dwi5` | Housing Maintenance Code Violations | 11.2M | novid, novdescription, novtype, boro, housenumber, low/highhousenumber, streetname, zip, apartment, class, inspectiondate, originalcorrectbydate, currentstatus, currentstatusdate | ✅ |
| `43nn-pn8j` | Restaurant Inspection Results | 295K | camis, dba, boro, building, street, zipcode, cuisine_description, inspection_date, action, violation_code, critical_flag, score, grade, phone, bbl | ✅ grade changes |
| `p937-wjvj` | Rodent Inspection | 3.1M | job_id, inspection_date, inspection_type, borough, zip_code, block, lot, result | ✅ |
| `tg4x-b46p` | Film Permits | — | eventid, eventtype, start/enddatetime, parkingheld, borough, zipcode_s, communityboard_s, policeprecinct_s, category, subcategoryname | ✅ |
| `xnfm-u3k5` | Street Resurfacing Schedule | — | boroughname, date, day, shifttype, onstreetname, fromstreetname, tostreetname, worktype, crewtype, communityboard | ✅ weekly |
| `tqtj-sjs8` | Street Construction Permits | — | permitnumber, permitser…, permitissuedate, issuedworkstartdate, issuedworkenddate, boroughname, onstreetname, fromstreetname, permitpurposecomments | ✅ |
| `dg92-zbpx` | City Record Online | 1.1M | request_id, start_date, end_date, agency_name, short_title, section_name, additional_description_1 | ✅ daily |
| `kpav-sd4t` | Jobs NYC Postings | 2.7K | business_title, civil_service_title, job_category, salary_range_from/to, work_location, posting_date, post_until, minimum_qual_requirements, preferred_skills | ✅ |
| `ez4e-fazm` | Bus Breakdown and Delays (school buses) | 1.3M | bus_no, route_number, reason, occurred_on, boro, bus_company_name, breakdown_or_running_late, has_contractor_notified_parents | ✅ |
| `5uug-f49n` | Harbor Water Quality | — | sampling_location, sample_date, weather_condition, pH, salinity, dissolved oxygen, enterococci/fecal coliform | ✅ samples |
| `hn5i-inap` | Forestry Tree Points | — | globalid, genusspecies, dbh, tpcondition, tpstructure, plantingspaceglobalid, geometry | ✅ |
| `693u-uax6` | Parking Meters Locations and Status | — | meter_number, status, meter_hours, on_street, from_street, to_street, side_of_street, pay_by_cell_number, borough, lat, long | ✅ status |
| `ic3t-wcy2` | DOB Job Application Filings | — | job/bbl/bin, borough, block, lot, building_type, existing_dwelling_units, enlargement_sq_footage, approved, efiling_filed | ✅ |
| `h9gi-nx95` | Motor Vehicle Collisions — Crashes | 2.3M | collision_id, crash_date, crash_time, on/off_street_name, persons/cyclist/pedestrian/motorist injured+killed, contributing_factor_vehicle_1 | ⚠️ frozen since 2026-06-15 |

### 8b. New candidates (found in this sweep, fields probed from live rows)

| ID | Dataset | Rows | Fields | Skill idea | Watch? |
|---|---|---|---|---|---|
| `aq7i-eu5q` | FloodNet: Street Flooding Events (sensors) | 3,269 | sensor_id, sensor_name, flood_start_time, flood_end_time, max_depth_inches, duration_mins, duration_above_4/12/24_inches_mins, drain_time_mins | **"Is my street flooded right now?"** — nearest sensor + depth | ✅ flooding events |
| `bkwf-xfky` | Drinking Water Quality — Distribution Monitoring | 172,399 | sample_date, sample_time, sample_site, residual_free_chlorine_mg_l, turbidity_ntu, coliform_quanti_tray_mpn_100ml, e_coli_quanti_tray_mpn_100ml | "How is my tap water testing this month?" | ✅ sampling |
| `592z-n7dk` | Bicycle Parking | 37,962 | onstreet, fromstreet, sid, racktype, program, date_inst, borough, borocd, latitude, longitude | "Where can I lock my bike near here?" | ✅ new racks |
| `ji82-xba5` | Facilities Database | 34,446 | facname, factype, facgroup, facdomain, overagency, opname, address, borough, zipcode, capacity, policeprct, schooldist, lat/long | "What city services are near me?" (fire, school, library, health) | ✅ |
| `kh3d-xhq7` | Queens Library Branches | 66 | name, address, city, postcode, borough, phone, latitude, longitude | "Closest library branch + phone" (Brooklyn/NYPL equivalents are older) | ❌ static |
| `ct66-47at` | Bicycle and Pedestrian Counts | 21.2M | sensor_id, timestamp, travelmode, counts, direction, granularity, status | "How busy is this bike lane / crossing, by hour" | ✅ |
| `mrjc-v9pm` | Flood Vulnerability Index | 2,209 | geoid, fshri, ss_80s | "Is this block flood-prone?" (by geoid, not address) | ❌ static |
| `kcfe-uypz` | Tree Contract Work Details | 346,359 | work_order_id, contract_number, line_item_description, units_used, unit_price, total_cost, transaction_date | "What tree work did the city pay for on my block?" (costs, not addresses) | ✅ |
| `hxm3-23vy` / `jsrs-ggnx` | E-Designations (flood risk) | — | (designation + spatial) | "Is my building in a flood-hazard area?" for insurance talk | ❌ static |

### 8c. Verdict

The best *new* skill is **street flooding right now** (`aq7i-eu5q`): it is the only roadmap-quality dataset that is genuinely near-real-time and event-shaped, which makes it the strongest demo of A2A push notifications outside 311. Runner-up: **tap water quality** (`bkwf-xfky`) — 172K rows, updated 2026-09-21, and nobody has built a water agent.

Everything else in 8b is static or facility data: good reference skills, weak watch/push skills.

Two of the earlier §3 limits still hold after this sweep: there is **no** public NYC API for *filing* a 311 complaint, and **no** MTA subway real-time arrivals in NYC Open Data (separate agency, keyed feeds).

---

## 9. Non-NYC datasets, verified for the later servers

Each row was probed live with a real request returning real rows (that is how the field lists and
quirks in the servers' `data.py` docstrings were written, not from documentation). The rows for
NWS through WFIGS were probed on 2026-09-22; the last four were probed on 2026-09-23. All keyless.

| Dataset | Endpoint | Verified with | Fields used | Server |
|---|---|---|---|---|
| NWS active alerts | `api.weather.gov/alerts/active` | county-wide query + counts | id, event, severity, onset, ends, headline, instruction, areaDesc | `servers/nws` |
| USGS earthquakes | `earthquake.usgs.gov/fdsnws/event/1/query` (GeoJSON) | a real event + counts | ids, mag, place, time, coordinates, depth, tsunami flag, felt/alerts | `servers/quakes` |
| NOAA CO-OPS predictions, observations, stations | `api.tidesandcurrents.noaa.gov` | station catalogue (3,499) + a real prediction series | t/v (time, value), sigma, flags, station metadata, flood stages | `servers/tides` |
| openFDA drug / food / device enforcement | `api.fda.gov/{drug,food,device}/enforcement.json` | `count=classification.exact` + a real record | recall_number, classification, product_description, reason_for_recall, status, report_date, distribution_pattern | `servers/recalls` |
| NOAA SWPC: Kp (1-minute, 3-hourly, forecast), alerts, OVATION | `services.swpc.noaa.gov/products/noaa-planetary-k-index.json`, `.../-forecast.json`, `.../alerts.json`, `/json/planetary_k_index_1m.json`, `/json/ovation_aurora_latest.json` | live rows for each, including the 65,160-cell OVATION grid | time_tag, Kp, estimated_kp, a_running, observed/predicted, product_id/issue_datetime/message, coordinates [lon, lat, probability] | `servers/aurora` |
| NIFC WFIGS current incidents | `services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0/query` | `returnCountOnly` (450 rows), a state filter, and a 100-mile distance query | IncidentName, IncidentSize, PercentContained, FireDiscoveryDateTime (epoch ms), IncidentTypeCategory, POOState, POOCounty, FireCause, GACC, UniqueFireIdentifier, geometry x/y | `servers/fire` |
| Open-Meteo air quality | `air-quality-api.open-meteo.com/v1/air-quality` | point and city queries, 1-hour and 6-hour forecasts, and the bad-request path | current + hourly US AQI, pm2_5, pm10, ozone, nitrogen_dioxide, sulphur_dioxide, carbon_monoxide, uv_index, alder/birch/grass/ragweed/mugwort/olive pollen; multiple coordinates return an array | `servers/air` |
| USGS Volcano Science Center alert levels | `volcanoes.usgs.gov/vsc/api/volcanoApi/elevated` and `.../geojson` | the live elevated list, plus the 161-volcano geojson catalogue | volcano_name, alert_level, color_code, observatory, region, threat_ranking, latitude/longitude, notice synopsis | `servers/volcanoes` |
| NOAA NDBC real-time observations | `www.ndbc.noaa.gov/data/latest_obs/latest_obs.txt`, `/data/realtime2/<id>.txt`, `www.ndbc.noaa.gov/activestations.xml` | the 872-row latest-observation table, one station's history file, and the 1,354-station catalogue | station id, time, WDIR/WSPD/GST/WVHT/DPD/APD/MWD/PRES/ATMP/WTMP/DEWP/VIS/TIDE, station name, owner, programme, sensors, lat/lon | `servers/buoys` |
| FAA NAS airport status | `www.fly.faa.gov/flyfaa/xmlAirportStatus.jsp` | a live snapshot (~1.8 KB) with both its delay and closure blocks | ARPT, Update_Time, Delay_type (arrival/departure/ground/closure), Reason, Arrival/Departure delay ranges, closure start and reopen | `servers/airports` |

Quirks worth remembering (all handled in the servers):

- SWPC ships bare JSON arrays for four of the five products, mixes observed and predicted rows in
  one forecast file, and serves a ~900 KB aurora grid.
- WFIGS answers unknown or malformed `where` clauses with a JSON `error` object, not an HTTP error;
  its dates are epoch milliseconds; the layer caps a response at 2,000 rows.
- An unknown make or model on NHTSA's recall API answers HTTP 400 with an empty result set (used by
  the `acp` repo's vehicles agent, same idea).
- Open-Meteo signals a bad request with HTTP 400 and `{"error": true, "reason": "…"}` — `error` is a
  boolean, not a string, so a client that only tests `payload.get("error")` for truthiness will treat
  the failure body as data. The air server overrides the kit's HTTP hook for that reason.
- The Volcano Science Center serves only `elevated` and `geojson`: the legacy
  `volcanoApi/volcanoes`, `/status` and `/volcanoDetails` paths 404, and the Smithsonian GVP answers
  403. A `geojson` read measured 12-76 s on the same document, so the server allows 90 s and caches
  for 15 minutes. Coordinates in the geojson are `[longitude, latitude]`, the reverse of the
  elevated list's named fields — worth checking before mapping either one.
- NDBC's station catalogue is at `/activestations.xml`; `/data/activestations.xml` 404s. The
  latest-observation table is one row per station with `MM` in a column whose sensor is out (so a
  station can publish wind and no sea state), and the per-station `/data/realtime2/<id>.txt` files are
  newest-first, unlike the table.
- The FAA status document is one XML snapshot whose two blocks can both be named "Airport Closures"
  when a delay programme and a closure are reported together; it lists only airports with something to
  report, so absence means "not affected", not "on time".
