# Noctalix Nowcast — `nowcast.noctalix.com`

PWA di allerta precoce (nowcasting 0–60 min) per nubifragi, celle temporalesche e grandine, con avvisi Web Push geolocalizzati.
Copertura: Italia (radar DPC, 1 km), Europa (radar EUMETNET OPERA, 1 km), resto del mondo (RainViewer); allerte ufficiali MeteoAlarm (38 paesi europei) e NWS (USA); fulmini MTG (Europa/Africa/Atlantico) e GOES GLM (Americhe).
Specifica completa: [`ARCHITECTURE_AND_SPEC.md`](ARCHITECTURE_AND_SPEC.md).

```
frontend/  Next.js 15 static export + Tailwind + MapLibre GL  → Cloudflare Pages (progetto "nowcast")
backend/   FastAPI + worker pySTEPS (LK + S-PROG) + PostGIS + Redis → VPS (docker compose)
```

## Pipeline (ogni nuovo scan radar, polling 60 s)

1. `ingest_dpc.py` – ultimi 6 frame **VMI** DPC (API REST dal 12/01/2026: `GET /findLastProductByType`, `POST /downloadProduct` → URL S3, header `Origin` obbligatorio), 1 km / 5 min, + **POH** (probabilità di grandine); maschera neve da RainViewer. Fallback: RainViewer (tile "Universal Blue" decodificate in dBZ, 10 min). Controllo qualità: clamp 70 dBZ, filtro clutter statico.
2. `nowcast_engine.py` – filtro < 15 dBZ, Z-R Marshall-Palmer (a=200, b=1.6), optical flow Lucas-Kanade, **S-PROG** 12×5 min.
3. `vectorizer.py` – contorni 35/45/55 dBZ, buffer `1.5 + 0.1·lead` km (EPSG:3035), PostGIS `forecast_polygons` (retention 2 h), PNG Web-Mercator dei frame −25…+60 min, `latest.json` + `latest_forecast.geojson` (gzip) su R2.
3b. `probability.py` + `points.py` – **nowcast probabilistico calibrato**: probabilità di superamento per evento e anticipo (Lagrangiano locale, Germann & Zawadzki), mappate con tabelle di affidabilità da backtest (`worker/data/calibration.json`, vedi [`docs/CALIBRATION.md`](docs/CALIBRATION.md)); previsione e osservazione **sul punto di ogni iscritto**; ogni avviso viene verificato col radar osservato (`GET /api/v1/verification`).
4. `notifier.py` – query `ST_Intersects`, priorità al lead time più breve, dedup 45 min con *upgrade* di severità, quiet hours (solo MODERATE silenziati, nel fuso dell'utente), rimozione endpoint 404/410, lock Redis anti-duplicati.
5. `lightning.py` – fulmini da **EUMETSAT MTG Lightning Imager** (LI-2-LFL, collezione `EO:EUM:DAT:0691`, licenza NoConditions): prodotti ogni 10 min, area Europa + regioni attive; **NOAA GOES-19/18 GLM** (bucket AWS pubblico, file ogni 20 s) solo mentre c'è una regione attiva nelle Americhe. Tabella `lightning_flashes` (3 h), `lightning.geojson`, avviso "Fulmini vicini" (≥2 fulmini entro 15 km in 20 min, tendenza avvicinamento) e menzione dei fulmini negli avvisi radar. MTG richiede secret `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET`. Asia/Oceania: nessuna fonte gratuita. Blitzortung non usato: vieta l'accesso da app di terzi senza approvazione.
6. `cap.py` – allerte ufficiali ogni 10 min: API JSON MeteoAlarm per l'Italia e per i paesi in cui l'app è usata (iscritti + visitatori recenti), NWS `alerts/active?point=` negli USA. Geometria: poligoni CAP, altrimenti aree EMMA_ID (`worker/data/emma_areas.geojson.gz`, caricate in `cap_areas`) o zone NWS (scaricate una volta e messe in cache). Push una tantum per allerta, una sola per tipo di rischio (niente doppioni per avvisi sovrapposti).
7. **Regioni fuori Italia** (`ingest_regional.py`, `pipeline.run_regional`, ogni 60 s): per ogni cella di 1° con iscritti fuori dalla copertura DPC o visitatori recenti (`coverage_demand`, posizione arrotondata a ~10 km, nessuna identità) viene calcolato un nowcast completo (stessa catena: LK + S-PROG, probabilità calibrate, avvisi sul punto) su un quadrato di ~400 km: radar **OPERA CIRRUS** (DBZH 1 km/5 min, CC BY 4.0, cache S3 ORD anonima, lettura a finestra del COG) in Europa, **RainViewer** altrove. Max `MAX_REGIONAL_DOMAINS` (6). Mappa: `regions.json` con i frame previsti di ogni regione + composito OPERA Europa osservato (panoramica 4 km); fuori Europa la PWA mostra le tile RainViewer osservate.

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
| GET | `/api/v1/alerts/point?lat=&lon=` | impatti previsti (tutte le regioni) + allerte CAP su un punto; registra la cella (~10 km) come zona da coprire |

Chiavi VAPID: se `VAPID_*` è vuoto vengono generate al primo avvio e salvate in `/data/vapid.json` (volume), condivise da api e worker.

## Sviluppo locale

```bash
cp .env.example .env
docker compose up -d --build            # db, redis, api (:8000), worker  (sviluppo)
docker compose run --rm worker python -m pytest -q
docker compose run --rm worker python -m tests.e2e_synthetic   # pipeline completa su dati sintetici
cd frontend && npm ci && NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Senza credenziali R2 gli asset vengono scritti in `/data/assets` e serviti dall'API su `/assets`.

## Deploy — a costo zero

| Componente | Dove | Costo |
|---|---|---|
| Frontend PWA | Cloudflare Pages (Git integration, build su Cloudflare, watch path `frontend/*`) | 0 (Free: 500 build/mese, oltre falliscono, nessun addebito) |
| Database PostGIS | Supabase Free, progetto esistente, **schema `nowcast` + ruolo `nowcast` isolati**, RLS attivo | 0 (Free: 500 MB, nessuna carta) |
| API + worker pySTEPS | Oracle Cloud Always Free **VM.Standard.E2.1.Micro** (AMD, 1 GB RAM + 4 GB swap), un solo container (`docker-compose.vps.yml`) + Caddy (TLS Let's Encrypt) | 0 (Always Free, senza upgrade a Pay As You Go) |
| Asset radar | serviti dall'API dietro proxy Cloudflare con `Cache-Control` (R2 opzionale, disattivato) | 0 |

Redis non è usato in produzione (store in-process, dedup durevole in `alert_history`); resta nel `docker-compose.yml` di sviluppo.

### GitHub Actions (minuti minimi)

| Workflow | Trigger | Minuti |
|---|---|---|
| `ci.yml` | solo manuale | 0 se non lanciato |
| `deploy-pages.yml` | solo manuale (fallback: normalmente builda Cloudflare) | 0 se non lanciato |
| `deploy-backend.yml` | push su `backend/**`, `docker-compose.vps.yml`, `Caddyfile` | 0 finché manca la variabile `VPS_HOST`; poi <1 min (build Docker in background sul VPS) |

Configurazione (Settings → Secrets and variables → Actions):

| Tipo | Nome | Valore |
|---|---|---|
| Variable | `VPS_HOST` | IPv4 pubblico VPS (crearla per ultima: abilita il deploy) |
| Secret | `VPS_SSH_KEY` | chiave privata SSH |
| Secret | `VPS_USER` | `ubuntu` |
| Secret | `DATABASE_URL` | `postgresql+asyncpg://nowcast.<ref>:<pw>@aws-1-eu-north-1.pooler.supabase.com:5432/postgres?ssl=require` (pooler in *session mode*, porta 5432) |
| Secret | `EUMETSAT_CONSUMER_KEY`, `EUMETSAT_CONSUMER_SECRET` | fulmini (account gratuito EUMETSAT) |
| Secret | `CLOUDFLARE_API_TOKEN` (opz.) | *Zone DNS: Edit* → record `api-nowcast` automatico |

Chiavi VAPID: generate al primo avvio e persistite nel volume `nowcast_data` del VPS.

Consigliato: GitHub → Settings → Billing → Budgets → budget Actions 0 $ con "Stop usage".

## Note / differenze rispetto alla specifica

- Repository effettivo: `simogerardini/nowcasting`.
- Colonne aggiuntive: `user_subscriptions.timezone` (quiet hours nel fuso utente), `alert_history.source/ref` (dedup CAP); tabelle `cap_alerts`, `pipeline_runs`.
- Dedup: blocca severità uguale **o inferiore** nei 45 min (la query della spec lasciava passare i downgrade).
- Allerte CAP: MeteoAlarm usa geocode EMMA_ID quasi sempre senza poligoni: i confini (2003 aree, 36 paesi) vengono da `geocodes.json` del pacchetto MIT `meteoalarm` (rigenerabili con `backend/scripts/build_cap_areas.py`); UK/Svizzera/Ucraina pubblicano poligoni.
- Blitzortung (fase 2) non implementato.
- Calibrazione probabilistica stimata sul radar DPC, applicata anche a OPERA e RainViewer; la verifica online (`/api/v1/verification`) misura l'affidabilità reale anche fuori Italia.
- Zone senza radar (gran parte di Africa, Asia centrale, oceani): RainViewer non distingue "asciutto" da "non coperto", quindi lì non arrivano avvisi radar.
- Fonte radar attiva e motivo di eventuale fallback visibili in `/api/v1/health` (`last_run.source`, `worker.dpc_error`).
