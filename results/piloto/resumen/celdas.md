Mediana de las repeticiones; entre paréntesis, mínimo–máximo. Latencias en ms, solo de repeticiones completas (`ok` o `spill`). M3: Q1 lee 500,000 documentos por consulta y Q2 53,941.

| Motor | Perfil | Consulta | Clientes | Estado | QPS | P95 | P99 | Throttling (s) | RSS máx (MiB) | Working set máx (MiB) | Spill | Muestras mín. | CPU ms / 1k docs | RSS MiB / 1k docs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| MongoDB | C | Q1 | 50 | ok×1 | 0.333 | 137,494.8 | 140,493.7 | 3,235.0 | 760.7 | 965.6 | false×1 | 100 | 6.01 | 0.030 |
| PostgreSQL JSONB | C | Q1 | 50 | oom×1 | 0.000 | — | — | 3,103.8 | 957.2 | 1,022.7 | sin lectura×1 | 0 | — | — |
