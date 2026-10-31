# Resultados y evidencia

Todo lo de esta carpeta lo escriben los scripts; nada se edita a mano. Permite recalcular y cuestionar
cada número del reporte.

| Ruta | Contenido | Lo escribe |
|---|---|---|
| `dataset_manifest.json` | Parámetros, distribuciones y sha256 del dataset | `synthetic_data_generator.py generate` |
| `carga_mongo.json`, `carga_postgres.json` | Documentos, tamaños, TOAST y tiempos de carga | `synthetic_data_generator.py load` |
| `entorno.txt` | Especificaciones del host y verificación de cgroups el día de las corridas | `run_benchmarks.sh check` |
| `explain/` | Planes de ejecución: `explain("executionStats")` y `EXPLAIN (ANALYZE, BUFFERS)` | `benchmark_runner/verificar.py` |
| `explain/variantes/` | Planes de Q1 y Q2 con cada variante de memoria del playbook | `verificar.py variantes` |
| `piloto/` | Celdas piloto (Q1, perfil C, 50 clientes) con el mismo formato que la matriz | `run_benchmarks.sh piloto` |
| `variantes/` | Experimento complementario: Q1 en PostgreSQL con `work_mem` de 64 MB (mismo esquema de CSV) | `run_benchmarks.sh variantes` |
| `mediciones.csv` | Una fila por corrida, con el esquema fijo del playbook | `benchmark_runner` |
| `runs/<id>/` | Datos crudos de cada corrida (ver abajo) | `benchmark_runner` |
| `matriz.log`, `variantes.log` | Salida de consola de la matriz y de las variantes | `run_benchmarks.sh` |
| `resumen/`, `variantes/resumen/` | Mediana por celda (`celdas.csv`, `celdas.md`) y gráficas | `benchmark_runner/consolidar.py` |

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
| `query_id` | `Q1` o `Q2` en la matriz (configuración de fábrica). En `variantes/mediciones.csv`, `Q1_WM64`: Q1 con `work_mem` de 64 MB |
| `concurrency` | Clientes simultáneos: 1, 10 o 50 |
| `run_number` | Repetición: 1, 2 o 3 |
| `throughput_qps` | Consultas **terminadas con éxito dentro de la ventana** ÷ duración de la ventana (300 s) |
| `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms` | Percentiles (rango más cercano) de las latencias de esas consultas, en ms. La latencia se mide en el cliente: desde que se envía la consulta hasta que se consume el último resultado |
| `cpu_throttled_seconds` | Segundos de CPU estrangulada del contenedor durante la ventana: diferencia de `throttled_usec` en `cpu.stat` (cgroups v2) entre el inicio y el fin, ÷ 10⁶. Es el mismo contador que expone cAdvisor como `container_cpu_cfs_throttled_seconds_total`; se lee directamente para que los límites de la ventana sean exactos, y la serie de cAdvisor queda en `runs/` para contrastar. **Suma el tiempo que espera cada proceso o hilo listo para correr**, así que puede superar la duración de la ventana (p. ej., 3 procesos de Postgres estrangulados durante 20 s ≈ 60 s). En `meta.json` quedan también `nr_periods` y `nr_throttled` para calcular la fracción de periodos estrangulados |
| `max_rss_mb` | Máximo de `container_memory_rss` (cAdvisor, cada 1 s) durante la ventana, en MiB |
| `disk_spill` | `true` si el motor escribió datos temporales durante la ventana. MongoDB: aumento de `metrics.query.sort.spillToDisk` o `metrics.query.group.spills` en `serverStatus`. PostgreSQL: aumento de `temp_files` en `pg_stat_database`. **Vacío** si no se pudieron leer los contadores al inicio o al fin de la ventana (por ejemplo, con el motor en recuperación tras un OOM); el motivo queda en `meta.json` |
| `status` | `oom` si el contenedor murió por falta de memoria (`State.OOMKilled`); si no, `error` si alguna consulta falló o ninguna terminó; si no, `spill` si `disk_spill` es `true`; si no, `ok` |

Si una corrida no completa ninguna consulta (por ejemplo, por OOM), `throughput_qps` vale 0 y las
columnas de latencia quedan vacías.

**OOM en PostgreSQL.** El OOM killer de cgroups suele matar un proceso hijo (un backend), no el proceso
principal: el contenedor sigue vivo, pero Postgres reinicia todos sus procesos y entra en recuperación.
Por eso `oom` se detecta con el contador `oom_kill` de `memory.events` y no solo con `State.OOMKilled`.

## Protocolo de cada corrida

1. Se recrea el contenedor del motor con los límites del perfil (la caché del motor arranca vacía) y se
   ejecuta `drop_caches` en la VM (caché del sistema vacía). El otro motor queda detenido.
2. Carga en **lazo cerrado**: N clientes (1, 10 o 50) en procesos separados, con una conexión cada uno,
   empiezan a la vez; cada cliente envía la siguiente consulta en cuanto termina la anterior. El motor
   siempre tiene N consultas en curso.
3. Calentamiento de 60 s con los mismos N clientes, ventana de medición de 300 s y enfriamiento de 60 s
   sin carga. Al cerrar la ventana, los clientes terminan la consulta que tengan en curso y no envían más.
4. Tiempo límite por consulta: 300 s (`maxTimeMS` en MongoDB, `statement_timeout` en PostgreSQL). Una
   consulta que lo excede cuenta como fallida.
5. Si el espacio libre del host baja de 8 GiB, se detiene el motor y la corrida queda como `error`.
6. Las corridas se ejecutan por rondas: primero la repetición 1 de todas las celdas, luego la 2 y la 3.

No se descartan corridas por tener pocas muestras: el número de consultas en la ventana queda en
`meta.json` y el reporte señala las celdas con menos de 30.

## `runs/<id>/`

`<id>` = `<engine>_<perfil>_<query_id>_c<concurrency>_r<run_number>`, por ejemplo `postgres_C_Q1_c50_r2`.

| Archivo | Contenido |
|---|---|
| `meta.json` | Identificadores de la celda; horas de inicio y fin de cada fase (limpieza de caché, calentamiento, medición, enfriamiento); imágenes con su digest; límites reales del contenedor (`docker inspect`); configuración de memoria del motor; consultas completadas y fallidas con sus mensajes; contadores de spill y throttling al inicio y al fin; máximo de `working_set` |
| `latencias.csv.gz` | Una fila por consulta: cliente, inicio y fin (ms epoch), latencia (ms), resultado (`ok` o el error), y si cayó dentro de la ventana |
| `metricas.csv.gz` | Series de cAdvisor cada 1 s del contenedor del motor durante toda la corrida: uso de CPU, CPU estrangulada, RSS y working set |
| `motor.log.gz` | Log del motor durante la corrida: archivos temporales de Postgres (`log_temp_files = 0`) y operaciones lentas de MongoDB (incluyen `usedDisk`). Es la evidencia de spill y OOM |

## Resumen de las repeticiones

Cada celda (motor × perfil × consulta × concurrencia) tiene 3 filas. El reporte usa la **mediana** de las
3 como valor de la celda y el mínimo y el máximo como barras de error. La mediana no se deja arrastrar
por una sola repetición atípica, y con 3 valores el rango muestra la dispersión completa.

`benchmark_runner/consolidar.py <carpeta>` lo calcula en `<carpeta>/resumen/`:
- Latencias y M3 (CPU y RSS por 1,000 documentos leídos) solo con repeticiones completas (`ok`/`spill`):
  las pocas consultas que terminan entre reinicios de un OOM no representan a la celda. QPS, throttling y
  memoria usan las 3 (un QPS cercano a 0 es el dato real de una celda con OOM).
- Además del RSS del CSV, el working set máximo (`runs/<id>/meta.json`): incluye memoria compartida
  (`shared_buffers`) y caché de páginas, y es lo que el cgroup compara con su límite.
- Gráficas: P99 frente a concurrencia, throttling frente a QPS, RSS y working set frente a concurrencia, y
  sus curvas en el tiempo en la celda más exigente (perfil C, 50 clientes).

## Variantes de memoria

El playbook propone variar la memoria en Q2 (MongoDB `allowDiskUse` false/true; PostgreSQL `work_mem`
4 MB/64 MB). Los planes de ejecución muestran que con nuestras consultas solo una de las cuatro
combinaciones cambia algo [`explain/variantes/`]:

| Variante | Q1 | Q2 |
|---|---|---|
| MongoDB `allowDiskUse` false / true | Igual: `$group` de 18 KB, sin volcado | Igual: ordena 1.4 MB (límite de 100 MB), sin volcado |
| PostgreSQL `work_mem` 4 MB / 64 MB | **Cambia**: con 64 MB el `Incremental Sort` cabe en memoria, pero el ordenamiento de las filas completas sigue volcando ~230 MB por proceso; los temporales escritos bajan de ~1.7 GB a ~0.8 GB | Igual: top-N heapsort de 275 kB |

Por eso la matriz se mide con la configuración de fábrica (en MongoDB 8, `allowDiskUse` es `true` por
defecto; en PostgreSQL, `work_mem` es 4 MB), y la única variante con efecto se mide aparte: Q1 en
PostgreSQL con `work_mem` de 64 MB en los 3 perfiles y las 3 concurrencias, con 3 repeticiones
(`variantes/mediciones.csv`). El valor se fija por sesión (`SET work_mem`), como lo haría una aplicación
para un reporte pesado.

