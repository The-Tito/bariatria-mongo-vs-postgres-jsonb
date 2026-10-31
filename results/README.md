# Resultados y evidencia

Todo lo de esta carpeta lo escriben los scripts; nada se edita a mano. Permite recalcular y cuestionar
cada número del reporte.

| Ruta | Contenido | Lo escribe |
|---|---|---|
| `dataset_manifest.json` | Parámetros, distribuciones y sha256 del dataset | `synthetic_data_generator.py generate` |
| `carga_mongo.json`, `carga_postgres.json` | Documentos, tamaños, TOAST y tiempos de carga | `synthetic_data_generator.py load` |
| `entorno.txt` | Especificaciones del host y verificación de cgroups el día de las corridas | `run_benchmarks.sh check` |
| `explain/` | Planes de ejecución: `explain("executionStats")` y `EXPLAIN (ANALYZE, BUFFERS)` | `benchmark_runner` |
| `mediciones.csv` | Una fila por corrida, con el esquema fijo del playbook | `benchmark_runner` |
| `runs/<id>/` | Datos crudos de cada corrida (ver abajo) | `benchmark_runner` |

## `mediciones.csv`

Esquema fijo del playbook (§8.2), **sin columnas agregadas ni renombradas**:

```
timestamp_epoch,engine,profile,query_id,concurrency,run_number,throughput_qps,latency_p50_ms,latency_p95_ms,latency_p99_ms,cpu_throttled_seconds,max_rss_mb,disk_spill,status
```

| Columna | Definición |
|---|---|
| `timestamp_epoch` | Segundos Unix (UTC, entero) al **inicio** de la ventana de medición de 300 s |
| `engine` | `mongo` o `postgres` |
| `profile` | `PROFILE_A_BASELINE`, `PROFILE_B_MEDIUM` o `PROFILE_C_SEVERE` |
| `query_id` | `Q1` o `Q2`. La codificación de las variantes de memoria de Q2 se define después de la celda piloto |
| `concurrency` | Clientes simultáneos: 1, 10 o 50 |
| `run_number` | Repetición: 1, 2 o 3 |
| `throughput_qps` | Consultas **terminadas con éxito dentro de la ventana** ÷ duración de la ventana (300 s) |
| `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms` | Percentiles (rango más cercano) de las latencias de esas consultas, en ms. La latencia se mide en el cliente: desde que se envía la consulta hasta que se consume el último resultado |
| `cpu_throttled_seconds` | Segundos de CPU estrangulada del contenedor durante la ventana: diferencia de `throttled_usec` en `cpu.stat` (cgroups v2) entre el inicio y el fin, ÷ 10⁶. Es el mismo contador que expone cAdvisor como `container_cpu_cfs_throttled_seconds_total`; se lee directamente para que los límites de la ventana sean exactos, y la serie de cAdvisor queda en `runs/` para contrastar |
| `max_rss_mb` | Máximo de `container_memory_rss` (cAdvisor, cada 1 s) durante la ventana, en MiB |
| `disk_spill` | `true` si el motor escribió datos temporales durante la ventana. MongoDB: aumento de `metrics.query.sort.spillToDisk` o `metrics.query.group.spills` en `serverStatus`. PostgreSQL: aumento de `temp_files` en `pg_stat_database` |
| `status` | `oom` si el contenedor murió por falta de memoria (`State.OOMKilled`); si no, `error` si alguna consulta falló o ninguna terminó; si no, `spill` si `disk_spill` es `true`; si no, `ok` |

Si una corrida no completa ninguna consulta (por ejemplo, por OOM), `throughput_qps` vale 0 y las
columnas de latencia quedan vacías.

## `runs/<id>/`

`<id>` = `<engine>_<perfil>_<query_id>_c<concurrency>_r<run_number>`, por ejemplo `postgres_C_Q1_c50_r2`.

| Archivo | Contenido |
|---|---|
| `meta.json` | Identificadores de la celda; horas de inicio y fin de cada fase (limpieza de caché, calentamiento, medición, enfriamiento); imágenes con su digest; límites reales del contenedor (`docker inspect`); configuración de memoria del motor; consultas completadas y fallidas con sus mensajes; contadores de spill y throttling al inicio y al fin; máximo de `working_set` |
| `latencias.csv.gz` | Una fila por consulta: cliente, inicio y fin (ms epoch), latencia (ms), resultado (`ok` o el error), y si cayó dentro de la ventana |
| `metricas.csv.gz` | Series de cAdvisor cada 1 s del contenedor del motor durante toda la corrida: uso de CPU, CPU estrangulada, RSS y working set |
| `motor.log` | Log del motor durante la corrida: archivos temporales de Postgres (`log_temp_files = 0`) y operaciones lentas de MongoDB (incluyen `usedDisk`). Es la evidencia de spill y OOM |

## Resumen de las repeticiones

Cada celda (motor × perfil × consulta × concurrencia) tiene 3 filas. El reporte usa la **mediana** de las
3 como valor de la celda y el mínimo y el máximo como barras de error. La mediana no se deja arrastrar
por una sola repetición atípica, y con 3 valores el rango muestra la dispersión completa.
