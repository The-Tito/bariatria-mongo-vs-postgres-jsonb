Mediana de las repeticiones; entre paréntesis, mínimo–máximo. Latencias en ms, solo de repeticiones completas (`ok` o `spill`). M3: Q1 lee 500,000 documentos por consulta y Q2 53,941.

| Motor | Perfil | Consulta | Clientes | Estado | QPS | P95 | P99 | Throttling (s) | RSS máx (MiB) | Working set máx (MiB) | Spill | Muestras mín. | CPU ms / 1k docs | RSS MiB / 1k docs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| PostgreSQL JSONB | A | Q1_WM64 | 1 | spill×3 | 0.570 | 1,773.8 | 1,805.0 (1,794.6–1,816.6) | 0.0 | 293.9 | 614.0 | true×3 | 170 | 5.30 | 0.588 |
| PostgreSQL JSONB | A | Q1_WM64 | 10 | spill×3 | 0.963 | 12,090.5 | 13,176.3 (12,592.6–13,481.9) | 1,305.9 | 1,664.0 | 2,956.5 | true×3 | 288 | 7.11 | 0.333 |
| PostgreSQL JSONB | A | Q1_WM64 | 50 | oom×3 | 0.007 | — | — | 1,488.5 | 3,882.0 | 4,094.7 | sin lectura×1 true×2 | 0 | — | — |
| PostgreSQL JSONB | B | Q1_WM64 | 1 | spill×3 | 0.470 | 2,696.5 | 3,053.1 (2,983.0–3,485.5) | 86.3 | 297.9 | 502.6 | true×3 | 141 | 5.41 | 0.596 |
| PostgreSQL JSONB | B | Q1_WM64 | 10 | spill×3 | 0.400 | 27,193.5 | 27,408.5 (26,423.9–29,205.0) | 2,057.0 | 1,634.3 | 1,854.9 | true×3 | 118 | 9.57 | 0.327 |
| PostgreSQL JSONB | B | Q1_WM64 | 50 | oom×3 | 0.000 | — | — | 2,928.5 | 1,965.4 | 2,047.2 | sin lectura×3 | 0 | — | — |
| PostgreSQL JSONB | C | Q1_WM64 | 1 | spill×3 | 0.290 | 4,104.3 | 4,802.7 (4,054.8–5,212.3) | 335.1 | 301.7 | 538.3 | true×3 | 87 | 6.35 | 0.603 |
| PostgreSQL JSONB | C | Q1_WM64 | 10 | oom×3 | 0.000 | — | — | 2,364.7 | 974.0 | 1,023.8 | sin lectura×3 | 0 | — | — |
| PostgreSQL JSONB | C | Q1_WM64 | 50 | oom×3 | 0.000 | — | — | 3,312.4 | 957.0 | 1,023.2 | sin lectura×3 | 0 | — | — |
