# Calibrazione del nowcast probabilistico

Backtest su radar DPC (VMI, 2 km, 5 min) - addestramento: 2026-10-20, 2026-10-21, 2026-10-24, 2026-11-01, 2026-11-02, 2026-11-04, 2026-11-05, 2026-11-06, 2026-11-10, 2026-11-12, 2026-11-13, 2026-11-15 (95 run); validazione indipendente: 2026-10-23, 2026-11-14, 2026-11-16, 2026-11-17.

Verifica: un avviso e' corretto se l'evento e' osservato entro ~3 km e +-5 min dall'orario previsto (stesso criterio della verifica in produzione).

| Evento | slope (km/min) | soglia P calibrata | precisione (validazione) | eventi intercettati (validazione) | precisione (addestramento) | precisione modello precedente | intercettati modello precedente |
|---|---|---|---|---|---|---|---|
| RAIN_START | 1.0 | 0.5 | 90% | 60% | 94% | 84% | 68% |
| HEAVY_RAIN | 1.5 | 0.6 | 91% | 14% | 97% | 56% | 47% |
| CLOUDBURST | 1.5 | 0.4 | 92% | 7% | 93% | 37% | 27% |
| HAIL | - | 0.7 | n.d. (nessun caso >=55 dBZ nel periodo) | n.d. | n.d. | 28% | 14% |

Affidabilita' delle probabilita' (validazione, media prevista vs frequenza osservata): RAIN_START 3.03% vs 2.71%, HEAVY_RAIN 0.20% vs 0.15%, CLOUDBURST 0.01% vs 0.01%.

## Curve precisione / eventi intercettati (validazione)

**RAIN_START**: P>=0.2: 78%/79% · P>=0.3: 83%/73% · P>=0.4: 87%/66% · P>=0.5: 90%/60% · P>=0.6: 91%/53% · P>=0.7: 93%/46% · P>=0.8: 94%/38% · P>=0.9: 96%/26%

**HEAVY_RAIN**: P>=0.2: 66%/45% · P>=0.3: 74%/32% · P>=0.4: 79%/24% · P>=0.5: 84%/19% · P>=0.6: 91%/14% · P>=0.7: 96%/10% · P>=0.8: 99%/7% · P>=0.9: 99%/4%

**CLOUDBURST**: P>=0.2: 78%/14% · P>=0.3: 86%/10% · P>=0.4: 92%/7% · P>=0.5: 95%/5% · P>=0.6: 98%/4% · P>=0.7: 99%/2% · P>=0.8: 100%/1%

Ricalibrazione: `cd backend && python -m scripts.backtest_calibrate --train <giorni> --valid <giorni>`; in produzione l'affidabilita' reale e' in `GET /api/v1/verification`.
