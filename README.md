# Medición de métricas: MongoDB vs PostgreSQL JSONB · Proyecto Bariatría

Práctica de **Bases de Datos en la Nube** (IT2iD, UPChiapas, 2026). Se comparan MongoDB y PostgreSQL
(guardando el mismo documento en una columna `JSONB`) al ejecutar agregaciones sobre documentos anidados
bajo límites estrictos de CPU y memoria con Docker (cgroups v2).

El dataset y las consultas no son los del playbook: se adaptaron al proyecto propio, un **sistema de
gestión clínica bariátrica**. El documento medido es una **visita** de un paciente con sus estudios
embebidos.

## Preguntas que responden las métricas

- **M1** ¿Cómo cambian P95 y P99 al limitar CPU y memoria?
- **M2** ¿Qué motor gestiona mejor los búferes antes de volcar a disco o caer por OOM?
- **M3** ¿Cuál es la huella de CPU y memoria por cada 1,000 documentos procesados?

## Estructura

Sigue los entregables del playbook (8.1 repositorio y 8.2 CSV).

| Ruta | Contenido | Estado |
|---|---|---|
| `docker-compose.yml` | MongoDB 8.0.32 y PostgreSQL 18.4 con perfiles A/B/C parametrizados; telemetría (cAdvisor, Node Exporter, Prometheus a 1 s, configurado en `telemetry/`) | ✅ |
| `synthetic_data_generator.py` | Dataset de 500k visitas con semilla fija y su carga en ambos motores | ✅ |
| `benchmark_runner/` | Runner en Python multiproceso | ⏳ |
| `queries/` | Esquemas (validador `$jsonSchema`, DDL JSONB) e índices de ambos motores; Q1 y Q2 en MongoDB y SQL | 🔄 esquemas listos |
| `run_benchmarks.sh` | Automatiza la matriz de mediciones; `check` verifica el entorno | 🔄 `check` listo |
| `results/` | CSV con el esquema fijo, evidencia de spill/OOM, manifiesto del dataset y estado de la carga | 🔄 dataset y carga |

## Requisitos

- Docker con cgroups v2 (Linux nativo, o Docker Desktop / VM en macOS y Windows)
- Python 3.11 o superior, con las dependencias de `requirements.txt`

Antes de medir, ejecuta `./run_benchmarks.sh check` y guarda su salida: documenta el host y confirma que
cgroups v2 aplica los límites de CPU y memoria, el swap en cero y el registro de throttling.

## Reproducir

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run_benchmarks.sh check

# Dataset: 500k visitas, determinista. Compara el sha256 con results/dataset_manifest.json
.venv/bin/python synthetic_data_generator.py generate

# Carga del mismo archivo en cada motor (uno a la vez)
DB_CPUS=4 DB_MEM=4096m docker compose --profile mongo up -d --wait mongo
.venv/bin/python synthetic_data_generator.py load mongo
docker compose --profile mongo stop mongo

DB_CPUS=4 DB_MEM=4096m docker compose --profile postgres up -d --wait postgres
.venv/bin/python synthetic_data_generator.py load postgres
docker compose --profile postgres stop postgres
```

La carga en MongoDB pasa por el validador `$jsonSchema`: si el archivo no cumple el contrato de
`queries/README.md`, se rechaza y se reporta cada motivo.

