# Specifica Tecnica & Architettura: Noctalix Weather Nowcast (`nowcast.noctalix.com`)

> **Istruzioni per l'Agente AI di Sviluppo:** Questo documento costituisce la specifica tecnica completa del progetto. Segui rigorosamente l'architettura, lo stack, lo schema database e le milestone definite di seguito. Implementa il codice in modo modulare, idempotente e pronto per la produzione all'interno del repository GitHub **`simogerardini/nowcast`**.

## 1. Obiettivo del Sistema

Sviluppare una Progressive Web App (PWA) ad altissima precisione geolocalizzata per l'allerta precoce (**Nowcasting**) di eventi meteorologici imminenti (nubifragi, celle temporalesche intense, grandine, raffiche di vento).

* **Repository GitHub Ufficiale:** `https://github.com/simogerardini/nowcast` (`simogerardini/nowcast`)
* **Target Lead Time (Finestra di preavviso):** 10–60 minuti prima dell'impatto sulle coordinate dell'utente.
* **Risoluzione spaziale target:** \~1 km × 1 km (mosaico radar nazionale).
* **Frequenza di aggiornamento:** Ciclo completo di ingestione, inferenza e notifica ogni 5 minuti.
* **Dominio & Infrastruttura:** Sottodominio `nowcast.noctalix.com` gestito tramite Cloudflare (DNS, SSL, Pages, R2, Edge Caching).

## 2. Architettura Generale & Stack Tecnologico

Il sistema segue un'architettura **Event-Driven & Geospatial-First** divisa in 3 livelli principali:

### A. Livello Edge & Frontend (`nowcast.noctalix.com`)

* **Hosting:** Cloudflare Pages collegato direttamente al repository GitHub **`simogerardini/nowcast`** (cartella `frontend/`, deploy continuo su push al branch `main`).
* **Framework:** React / Next.js (Static Export) oppure SvelteKit + TypeScript + Tailwind CSS.
* **Mappa Interattiva:** MapLibre GL JS (open-source, prestazioni WebGL) con layer vettoriali GeoJSON per i poligoni di rischio e layer raster/overlay per i frame radar passati e previsti.
* **Notifiche & Background:**
  * Web Push API nativa tramite Service Worker (`sw.js`) e protocollo VAPID.
  * Geolocation API (`navigator.geolocation.watchPosition` / aggiornamento periodico in background al cambio significativo di coordinate > 500m).
  * **Gestione UX iOS Safari (Obbligatoria):** Su iOS/iPadOS, Apple consente le Web Push API *esclusivamente* se l'app è installata nella schermata Home (`window.navigator.standalone === true`). Il frontend deve rilevare iOS Safari non-standalone e mostrare un banner guida chiaro (*"Tocca Condividi -> Aggiungi a schermata Home per attivare gli avvisi meteo push"*).

### B. Livello Dati & Storage

* **Database Geospaziale:** PostgreSQL 16 + estensione **PostGIS 3.4**.
  * Indici spaziali `GIST` sulle geometrie degli utenti e sui poligoni delle celle meteo per query sub-millisecondo (`ST_DWithin`, `ST_Intersects`).
* **Cache & Code:** Redis (gestione code Celery/ARQ e deduplicazione notifiche tramite TTL).
* **Object Storage:** **Cloudflare R2** (compatibile S3 via `boto3`, zero costi di egress) per salvare i GeoJSON elaborati e le immagini PNG/WebP georeferenziate dei frame radar da servire direttamente al frontend via CDN.

### C. Motore di Calcolo & Nowcasting (Backend Worker)

* **Requisiti Hardware & Performance:** A differenza dei modelli Deep Learning su GPU, la pipeline `pySTEPS` (Optical Flow + S-PROG) gira interamente su **CPU** in circa **3–6 secondi** per l'intero mosaico nazionale italiano. Il container deve essere ottimizzato per esecuzione CPU su VPS standard (es. 2 vCPU, 4 GB RAM).
* **Linguaggio & API:** Python 3.11+ con **FastAPI** (gestione iscrizioni push, aggiornamento coordinate utente, stato sistema).
* **Task Scheduler:** Celery / APScheduler / Cron ogni 5 minuti sincronizzato con il rilascio dei dati radar.
* **Core Scientifico:**
  * `pySTEPS` (`pysteps`): libreria standard per nowcasting probabilistico a breve termine (Optical Flow Lucas-Kanade + modello stocastico **S-PROG**).
  * `rasterio` / `xarray` / `rioxarray` / `gdal`: lettura e riproiezione GeoTIFF/NetCDF.
  * `shapely` / `geopandas`: vettorizzazione dei campi di riflettività radar in poligoni GeoJSON con buffer di incertezza.
  * `pywebpush`: invio cifrato delle notifiche push VAPID.
  * `boto3`: upload dei file GeoJSON/raster su Cloudflare R2.

## 3. Pipeline di Ingestione e Predizione (Massima Precisione)

Il worker deve eseguire ogni 5 minuti la seguente pipeline:

### Step 1: Ingestione Dati Multi-Sorgente

1. **Dati Radar Grezzi (Primario - Italia):**
   * Sorgente: Dipartimento della Protezione Civile (DPC) - Open Data Radar (`radar-dpc`), mosaico nazionale riflettività **VMI** (*Vertical Maximum Intensity*) e/o **SRI** (*Surface Rainfall Intensity*) in formato GeoTIFF/NetCDF aggiornato ogni 5 minuti.
   * *Fallback / Estensione Europea:* EUMETNET OPERA composite o RainViewer API (per copertura fuori dai confini italiani o in caso di indisponibilità temporanea del feed DPC).
2. **Allerte Istituzionali CAP (Secondario - Sinottico):**
   * Sorgente: Feed XML/CAP (*Common Alerting Protocol*) della Protezione Civile / MeteoAlarm per intercettare allerte su vento forte, mareggiate o criticità idrogeologiche non visibili dal solo radar precipitativo.
3. **Telemetria Fulmini (Conferma Convettiva - Opzionale/Fase 2):**
   * Integrazione Blitzortung (WebSocket) per confermare che una cella ad alta riflettività sia effettivamente un temporale attivo e filtrare eventuali falsi echi radar (ground clutter / propagazione anomala).

### Step 2: Preprocessing & Filtraggio

* Scartare valori di riflettività $< 15\text{ dBZ}$ (rumore di fondo / pioviggine irrilevante).
* Convertire la riflettività $Z$ ($\text{dBZ}$) in tasso di precipitazione $R$ ($\text{mm/h}$) tramite relazione di Marshall-Palmer:
  $$Z = a R^b \quad (\text{standard } a = 200, \, b = 1.6)$$
* Classificare i livelli di severità in base ai $\text{dBZ}$:
  * `35 - 44 dBZ`: Pioggia forte / Rovescio intenso (`MODERATE` - Livello Giallo).
  * `45 - 54 dBZ`: Nubifragio / Temporale forte (`SEVERE` - Livello Arancione).
  * `>= 55 dBZ`: Alta probabilità di grandine / Downburst (`EXTREME` - Livello Rosso).

### Step 3: Inferenza Nowcasting con `pySTEPS`

* Acquisire gli ultimi **4–6 frame radar** consecutivi (ultimi 20–30 minuti) per stimare il campo vettoriale di movimento tramite Optical Flow (`LK` - Lucas-Kanade).
* Eseguire il modello **S-PROG** (*Spectral Prognosis*) per generare **12 step previsionali** a intervalli di 5 minuti (orizzonte totale: **+60 minuti**).
* *Motivazione tecnica S-PROG:* Filtra progressivamente le scale spaziali più piccole al crescere del lead time, eliminando il rumore ad alta frequenza e preservando la traiettoria coerente delle celle convettive.

### Step 4: Vettorizzazione & Cono di Incertezza

* Per ogni step previsionale ($t+5, t+10, \dots, t+60$):
  * Estrarre i contorni (isolinee) per le soglie $35, 45, 55\text{ dBZ}$ tramite `rasterio.features.shapes` + `shapely`.
  * Applicare un buffer dinamico proporzionale al lead time per compensare eventuali deviazioni di traiettoria:
    $$\text{buffer (km)} = 1.5 + 0.1 \times \text{minuti\_previsione}$$
  * Salvare i poligoni risultanti in PostGIS nella tabella `forecast_polygons` (cancellando i run più vecchi di 2 ore) e caricare il GeoJSON compresso su **Cloudflare R2** (`latest_forecast.geojson`).

## 4. Schema Database (PostgreSQL + PostGIS)

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

-- Tabella sottoscrizioni utenti e ultima posizione nota
CREATE TABLE user_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint TEXT UNIQUE NOT NULL,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    location GEOMETRY(Point, 4326) NOT NULL,
    min_severity_dbz INT DEFAULT 40, -- Soglia personalizzabile dall'utente
    quiet_hours_start TIME NULL,
    quiet_hours_end TIME NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_user_location ON user_subscriptions USING GIST (location);

-- Tabella poligoni di previsione generati dall'ultimo run
CREATE TABLE forecast_polygons (
    id SERIAL PRIMARY KEY,
    run_timestamp TIMESTAMPTZ NOT NULL,
    lead_time_minutes INT NOT NULL, -- es. 5, 10, 15... 60
    severity_level VARCHAR(16) NOT NULL, -- 'MODERATE', 'SEVERE', 'EXTREME'
    max_dbz FLOAT NOT NULL,
    estimated_rain_mmh FLOAT NOT NULL,
    geom GEOMETRY(MultiPolygon, 4326) NOT NULL
);

CREATE INDEX idx_forecast_geom ON forecast_polygons USING GIST (geom);

-- Log notifiche inviate per deduplicazione (evita spam ogni 5 minuti)
CREATE TABLE alert_history (
    id SERIAL PRIMARY KEY,
    subscription_id UUID REFERENCES user_subscriptions(id) ON DELETE CASCADE,
    severity_level VARCHAR(16) NOT NULL,
    sent_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_alert_dedup ON alert_history (subscription_id, sent_at);
```

## 5. Logica di Matching Spaziale & Alerting

Subito dopo aver popolato `forecast_polygons` per il run corrente, il worker esegue la query spaziale di intersezione:

```sql
SELECT DISTINCT ON (u.id)
    u.id AS user_id,
    u.endpoint,
    u.p256dh,
    u.auth,
    f.lead_time_minutes,
    f.severity_level,
    f.estimated_rain_mmh
FROM user_subscriptions u
JOIN forecast_polygons f
  ON ST_Intersects(u.location, f.geom)
WHERE f.run_timestamp = :current_run
  AND f.max_dbz >= u.min_severity_dbz
  AND NOT EXISTS (
      SELECT 1 FROM alert_history ah
      WHERE ah.subscription_id = u.id
        AND ah.sent_at > NOW() - INTERVAL '45 minutes'
        AND ah.severity_level = f.severity_level
  )
ORDER BY u.id, f.lead_time_minutes ASC;
```

### Regole di Invio Notifica:

1. **Priorità al tempo di arrivo più imminente:** `ORDER BY f.lead_time_minutes ASC`.
2. **Deduplicazione intelligente:** Nessun doppio invio per lo stesso livello di severità entro 45 minuti, a meno di un *upgrade* di intensità (es. passaggio da `MODERATE` a `EXTREME`/Grandine).
3. **Gestione Quiet Hours:** Se `quiet_hours_start` e `quiet_hours_end` sono impostati, silenziare le notifiche `MODERATE` durante la fascia notturna, mantenendo attive solo le notifiche `SEVERE` ed `EXTREME`.
4. **Pulizia endpoint scaduti:** Se `pywebpush` riceve HTTP `404` o `410 Gone` dal push service del browser, eliminare automaticamente il record da `user_subscriptions`.
5. **Payload Web Push Standard:**
   ```json
   {
     "title": "⚠️ Temporale forte in arrivo tra ~15 min",
     "body": "Prevista pioggia intensa (42 mm/h) sulla tua posizione alle 16:35. Tocca per aprire il radar live.",
     "icon": "/icons/storm-alert.png",
     "badge": "/icons/badge.png",
     "data": {
       "url": "https://nowcast.noctalix.com/?focus=alert"
     }
   }
   ```

## 6. Configurazione Infrastruttura Cloudflare (`noctalix.com`) & Variabili d'Ambiente

### DNS & Routing Cloudflare

1. `nowcast.noctalix.com` -> CNAME verso il progetto Cloudflare Pages collegato a `simogerardini/nowcast` (Frontend PWA).
2. `api-nowcast.noctalix.com` -> Record A/AAAA (Proxied, SSL Full Strict) verso la VPS che ospita FastAPI + PostGIS + Worker `pySTEPS`. Configurare CORS su FastAPI per accettare esclusivamente `https://nowcast.noctalix.com`.
3. `assets-nowcast.noctalix.com` -> Custom Domain collegato al bucket **Cloudflare R2** per servire i file GeoJSON e le immagini radar senza consumare banda della VPS.
4. **Regole Cache Cloudflare:** Su `assets-nowcast.noctalix.com/*`, impostare `Cache-Control: public, max-age=180` (3 minuti) in modo che i client ricevano sempre l'ultimo scan radar fresco direttamente dall'Edge.

### Variabili d'Ambiente Richieste (`.env`)

```env
# Database & Redis
DATABASE_URL=postgresql+asyncpg://noctalix:password@db:5432/nowcast
REDIS_URL=redis://redis:6379/0

# Web Push VAPID Keys
VAPID_PUBLIC_KEY=...
VAPID_PRIVATE_KEY=...
VAPID_CLAIM_EMAIL=mailto:admin@noctalix.com

# Cloudflare R2 (S3 Compatible)
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET_NAME=noctalix-nowcast-assets
R2_PUBLIC_URL=https://assets-nowcast.noctalix.com

# Frontend
NEXT_PUBLIC_API_URL=https://api-nowcast.noctalix.com
NEXT_PUBLIC_ASSETS_URL=https://assets-nowcast.noctalix.com
NEXT_PUBLIC_VAPID_PUBLIC_KEY=...
```

## 7. Struttura del Repository GitHub (`simogerardini/nowcast`)

```text
simogerardini/nowcast/
├── .github/
│   └── workflows/
│       └── deploy-pages.yml      # CI/CD automatico per frontend su Cloudflare Pages
├── frontend/                     # PWA (Next.js / SvelteKit + MapLibre GL)
│   ├── public/
│   │   ├── manifest.json         # Configurazione PWA installabile
│   │   ├── sw.js                 # Service Worker per Web Push & Offline
│   │   └── icons/                # Icone PWA (192x192, 512x512, badge)
│   └── src/
│       ├── components/
│       │   ├── RadarMap.tsx      # Mappa MapLibre con timeline -30m / +60m
│       │   ├── AlertSettings.tsx # Soglie dBZ, quiet hours, permessi GPS/Push
│       │   └── IosInstallBanner.tsx # Guida installazione Home Screen per iOS Safari
│       └── lib/
│           └── pushManager.ts    # Registrazione VAPID e sync coordinate
├── backend/                      # Python FastAPI + Motore pySTEPS
│   ├── app/
│   │   ├── main.py               # Endpoints REST (/subscribe, /location, /health)
│   │   ├── database.py           # Connessione asyncpg / SQLAlchemy + GeoAlchemy2
│   │   └── models.py
│   ├── worker/
│   │   ├── ingest_dpc.py         # Downloader radar Protezione Civile & feed CAP
│   │   ├── nowcast_engine.py     # Pipeline pySTEPS (Optical Flow + S-PROG)
│   │   ├── vectorizer.py         # Estrazione poligoni GeoJSON + upload su R2
│   │   └── notifier.py           # Query PostGIS e invio Web Push VAPID
│   ├── Dockerfile                # Immagine Python con libgdal-dev e dipendenze pysteps
│   └── requirements.txt
├── docker-compose.yml            # Orchestrazione PostGIS, Redis, FastAPI, Worker
├── .env.example
├── ARCHITECTURE_AND_SPEC.md      # Questo documento di specifica
└── README.md
```

## 8. Roadmap di Sviluppo (Milestone Incrementali)

1. **Milestone 1 (Infrastruttura & DB):**
   * Inizializzare il repo `simogerardini/nowcast` con `docker-compose.yml` (`postgis/postgis:16-3.4`, `redis:7-alpine`, servizio `api` FastAPI e servizio `worker`).
   * Implementare le tabelle SQL, la generazione automatica/script delle chiavi VAPID e gli endpoint REST `POST /api/v1/subscribe`, `PATCH /api/v1/location`, `DELETE /api/v1/unsubscribe` e `GET /api/v1/health`.
2. **Milestone 2 (Motore Radar & `pySTEPS`):**
   * Implementare `ingest_dpc.py` per scaricare gli ultimi 6 frame radar GeoTIFF del Dipartimento Protezione Civile (con fallback su API RainViewer/OPERA).
   * Implementare `nowcast_engine.py` con `pysteps` (Optical Flow Lucas-Kanade + S-PROG a 12 step / +60 min) e `vectorizer.py` per salvare i poligoni in PostGIS e caricare `latest_forecast.geojson` su Cloudflare R2.
3. **Milestone 3 (Motore di Notifica & Allerte CAP):**
   * Implementare `notifier.py` con la query `ST_Intersects`, gestione quiet hours, deduplicazione e invio notifiche tramite `pywebpush` (con rimozione automatica degli endpoint `410 Gone`).
   * Integrare il parser dei feed CAP Protezione Civile per allerte idrogeologiche/vento.
4. **Milestone 4 (Frontend PWA & CI/CD):**
   * Sviluppare la PWA in `frontend/` con mappa MapLibre GL, cursore temporale interattivo (da `-30m` a `+60m`), pannello impostazioni soglia $\text{dBZ}$, `IosInstallBanner.tsx`, `manifest.json` e `sw.js`.
   * Configurare `.github/workflows/deploy-pages.yml` su `simogerardini/nowcast` per il deploy automatico su Cloudflare Pages.