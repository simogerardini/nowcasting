# Noctalix Nowcast — `nowcast.noctalix.com`

PWA di allerta precoce (nowcasting 0–60 min) per nubifragi, celle temporalesche e grandine, con avvisi Web Push geolocalizzati.
Specifica completa: [`ARCHITECTURE_AND_SPEC.md`](ARCHITECTURE_AND_SPEC.md).

```
frontend/  Next.js 15 static export + Tailwind + MapLibre GL  → Cloudflare Pages (progetto "nowcast")
backend/   FastAPI + worker pySTEPS (LK + S-PROG) + PostGIS + Redis → VPS (docker compose)
```

## Pipeline (ogni nuovo scan radar, polling 60 s)

1. `ingest_dpc.py` – ultimi 6 frame **VMI** DPC (`radar-api.protezionecivile.it`), cache su disco; fallback RainViewer (tile dBZ grezzi).
2. `nowcast_engine.py` – filtro < 15 dBZ, Z-R Marshall-Palmer (a=200, b=1.6), optical flow Lucas-Kanade, **S-PROG** 12×5 min.
3. `vectorizer.py` – contorni 35/45/55 dBZ, buffer `1.5 + 0.1·lead` km (EPSG:3035), PostGIS `forecast_polygons` (retention 2 h), PNG Web-Mercator dei frame −25…+60 min, `latest.json` + `latest_forecast.geojson` (gzip) su R2.
4. `notifier.py` – query `ST_Intersects`, priorità al lead time più breve, dedup 45 min con *upgrade* di severità, quiet hours (solo MODERATE silenziati, nel fuso dell'utente), rimozione endpoint 404/410, lock Redis anti-duplicati.
5. `cap.py` – feed CAP MeteoAlarm Italia ogni 10 min → `cap_alerts` + push una tantum per allerta.

## API (`https://api-nowcast.noctalix.com`)

| Metodo | Path | |
|---|---|---|
| GET | `/api/v1/health` | stato DB/Redis + ultimo run |
| GET | `/api/v1/config` | chiave VAPID pubblica + URL asset (letta a runtime dalla PWA) |
| POST | `/api/v1/subscribe` | upsert sottoscrizione + posizione + preferenze |
| PATCH | `/api/v1/location` | aggiornamento coordinate (client: solo se spostamento > 500 m) |
| PATCH | `/api/v1/settings` | soglia dBZ / quiet hours / fuso |
| PATCH | `/api/v1/resubscribe` | rotazione endpoint dal service worker (`pushsubscriptionchange`) |
| DELETE | `/api/v1/unsubscribe` | |
| GET | `/api/v1/alerts/point?lat=&lon=` | impatti previsti + allerte CAP su un punto |

Chiavi VAPID: se `VAPID_*` è vuoto vengono generate al primo avvio e salvate in `/data/vapid.json` (volume), condivise da api e worker.

## Sviluppo locale

```bash
cp .env.example .env
docker compose up -d --build            # db, redis, api (:8000), worker
docker compose run --rm worker python -m pytest -q
docker compose run --rm worker python -m tests.e2e_synthetic   # pipeline completa su dati sintetici
cd frontend && npm ci && NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Senza credenziali R2 gli asset vengono scritti in `/data/assets` e serviti dall'API su `/assets`.

## Deploy — a costo zero

Obiettivo: **0 € oltre all'abbonamento Claude**. Limiti applicati:

| Workflow | Trigger | Minuti Actions |
|---|---|---|
| `ci.yml` | solo manuale (`workflow_dispatch`, scope backend/frontend/all) | 0 se non lanciato; timeout 15/8 min |
| `deploy-pages.yml` | solo manuale (fallback) | 0 se non lanciato; timeout 8 min |
| `deploy-backend.yml` | push su `backend/app|worker`, `Dockerfile`, `requirements.txt`, compose | job **saltato (0 min)** finché la variabile `VPS_HOST` non esiste; poi ~1 min (build Docker sul VPS); timeout 6 min |

Frontend: build e deploy sul **Cloudflare Pages Git integration** (build su Cloudflare, piano Free: 500 build/mese, nessun addebito oltre: le build in eccesso falliscono, non si pagano). Watch path `frontend/*`: i commit backend non generano build.

Asset radar: **R2 disattivato di default** (R2 richiede metodo di pagamento e addebita oltre il free tier). Gli asset sono serviti dall'API dietro il proxy Cloudflare Free con `Cache-Control` (PNG per-run immutabili 24 h, `latest.json` 60 s, GeoJSON 180 s): la cache edge assorbe il traffico dei client.
Se in futuro si abilita R2: ~210k scritture/mese (sotto 1M Class A gratuite), storage < 100 MB.

Già configurato su Cloudflare: progetto Pages `nowcast` (`nowcast-biv.pages.dev`), dominio `nowcast.noctalix.com` + CNAME proxied.
TLS API: Caddy ottiene certificato Let's Encrypt (HTTP-01 attraverso il proxy Cloudflare) → compatibile con SSL *Full (strict)*.

### Configurazione GitHub (Settings → Secrets and variables → Actions)

| Tipo | Nome | Uso |
|---|---|---|
| Variable | `VPS_HOST` | abilita il deploy backend |
| Secret | `VPS_SSH_KEY` (+ opz. `VPS_USER`, `VPS_PORT`) | SSH verso il VPS |
| Secret | `CLOUDFLARE_API_TOKEN` (opz.) | *Zone DNS: Edit* → record `api-nowcast` automatico; *Pages: Edit* per il fallback manuale |

Consigliato: GitHub → Settings → Billing → Budgets: budget Actions **0 $** con "Stop usage" attivo.

## Note / differenze rispetto alla specifica

- Repository effettivo: `simogerardini/nowcasting`.
- Colonne aggiuntive: `user_subscriptions.timezone` (quiet hours nel fuso utente), `alert_history.source/ref` (dedup CAP); tabelle `cap_alerts`, `pipeline_runs`.
- Dedup: blocca severità uguale **o inferiore** nei 45 min (la query della spec lasciava passare i downgrade).
- Allerte CAP Italia: MeteoAlarm usa geocode EMMA_ID senza poligoni; i poligoni vengono letti se presenti, altrimenti da un GeoJSON di geocode opzionale (`CAP_GEOCODES_URL` / `/data/cap_geocodes.geojson`). Senza questo file le allerte CAP non georeferenziate vengono ignorate.
- Blitzortung (fase 2) non implementato.
- Endpoint DPC/RainViewer implementati secondo le API pubbliche note; non verificabili dall'ambiente di sviluppo (rete bloccata) → controllare `/api/v1/health.last_run` dopo il primo deploy.
