# Esquemas y consultas

Ambos motores guardan **el mismo documento**: la visita de un paciente con sus estudios embebidos.
El generador produce cada visita una sola vez y la carga en los dos motores.

| Archivo | Contenido |
|---|---|
| `mongo/visitas.schema.json` | Validador `$jsonSchema` de la colección `visitas` (`validationLevel: strict`, `validationAction: error`) |
| `mongo/indexes.json` | Índices de `visitas`, además del índice automático sobre `_id` |
| `postgres/schema.sql` | Tabla `visitas (id uuid PRIMARY KEY, data jsonb)` |
| `postgres/indexes.sql` | Índice de fecha y `VACUUM (ANALYZE)` |
| `mongo/q1.json`, `postgres/q1.sql` | Q1 (P15): medicación crónica por año y fármaco |
| `mongo/q2.json`, `postgres/q2.sql` | Q2 (P14): Top-1000 de visitas cerradas de 2026 por IMC |

## Procedimiento de carga (idéntico en ambos motores)

1. Crear la colección o la tabla (en MongoDB, con su validador).
2. Insertar las 500k visitas.
3. Crear los índices.
4. En Postgres, `VACUUM (ANALYZE)`; MongoDB no usa estadísticas.

Los índices se crean después de la carga para que ninguno de los dos motores pague su mantenimiento
durante la inserción.

## Índices

| Motor | Índice | Para qué |
|---|---|---|
| MongoDB | `_id` (automático) | Llave del documento (`order_id` en el playbook) |
| MongoDB | `{ fecha: 1 }` | Rango de fechas de Q2 |
| PostgreSQL | `PRIMARY KEY (id)` | Llave del documento |
| PostgreSQL | `((data->>'fecha') COLLATE "C")` | Rango de fechas de Q2 |

No hay índice en `resumen.imc`, para que Q2 ordene en memoria. Tampoco se crea `{paciente_id, fecha}`:
ninguna consulta del experimento lo usa y ocuparía caché, lo que alteraría los resultados en el perfil C.

## Documento y conversiones

En MongoDB el documento se guarda con tipos BSON. En PostgreSQL se guarda en `data` como JSON, con estas
conversiones (las únicas que hacen falta, porque JSON no tiene esos tipos):

| BSON | En `data` (JSONB) | Ejemplo en JSONB | Motivo |
|---|---|---|---|
| UUID (`binData` subtipo 4) | texto, minúsculas con guiones; también en la columna `id uuid` | `"9b2f…-…"` | Llave primaria nativa; el orden de `uuid` coincide con el de `binData` (byte a byte) |
| `objectId` | texto hexadecimal de 24 caracteres | `"6714a1f0c3b2a10001a0b001"` | JSON no tiene ObjectId |
| `date` | texto ISO 8601 en UTC, con milisegundos y `Z` | `"2026-03-14T17:05:00.000Z"` | El ancho fijo hace que el orden de texto sea el cronológico; permite un índice inmutable |
| `int` | número entero | `132` | En SQL se lee con `::int` |
| `double` | número con decimales | `118.4` | En SQL se lee con `::float8`, que equivale al `double` de BSON (no `::numeric`) |

## Campos de la visita

Los rangos son límites de plausibilidad para detectar errores del generador, no rangos clínicos. Los que
vienen de RN-03 (peso, estatura, presión sistólica) son los ejemplos del proyecto; el resto son
**supuestos del experimento**.

| Campo | Tipo BSON | Obligatorio | Rango o valores |
|---|---|---|---|
| `_id` | UUID | sí | UUID v4 |
| `paciente_id` | objectId | sí | |
| `fecha` | date | sí | UTC |
| `tipo` | string | sí | `inicial`, `seguimiento` |
| `estado` | string | sí | `abierta`, `cerrada` |
| `cerrada_en` | date | solo si `cerrada` | |
| `datos_paciente.sexo` | string | sí | `F`, `M` |
| `datos_paciente.edad_anios` | int | sí | 18–100 |
| `resumen.peso_kg` | double | sí | 20–400 |
| `resumen.imc` | double | no: se omite si falta la estatura | 10–120 |
| `estudios[]` | array | sí | 1 a 4 estudios, cada uno de un tipo |
| `notas` | string | no | hasta 1000 caracteres |
| `dieta` | `{nombre, indicaciones}` | no | |
| `medicamentos[]` | `{nombre, categoria, dosis_mg, tomas_por_dia, duracion_dias}` | sí (puede ir vacío) | hasta 8; ver abajo |

### Tipos de estudio (`estudios[].tipo_estudio`)

| Tipo | Instrumento | `mediciones` | `resultados_calculados` |
|---|---|---|---|
| `antropometria` | Báscula y cinta | `peso_kg` (double), `estatura_cm` (double, opcional), `circunferencias_cm {cintura, cadera, muneca, brazo}` (double) | `imc` (solo con estatura), `indice_cintura_cadera` |
| `pliegues_cutaneos` | Pinza | `pliegues_mm {biceps, triceps, subescapular, suprailiaco}` (double, 1–80) | — |
| `signos_vitales` | Glucómetro y baumanómetro | `glucosa_mg_dl` (int, en ayuno, 30–600), `presion {sistolica, diastolica}` (int) | — |
| `bioimpedancia` | Báscula de bioimpedancia | `pct_grasa`, `masa_muscular_kg` (double), `grasa_visceral`, `edad_metabolica` (int) | — (son datos reportados por la báscula) |

Cada tipo aparece como máximo una vez por visita; eso lo garantiza el generador, porque `$jsonSchema` no
puede exigir valores únicos de un campo dentro de un arreglo. El validador rechaza campos no declarados
(`additionalProperties: false`) y tipos numéricos distintos: un `int` donde se espera `double` es un error.

### Medicación vigente (`medicamentos[]`)

Es el arreglo que desanida Q1. A diferencia de `estudios[]`, **todos sus elementos tienen la misma forma**,
como `items` en el playbook.

| Campo | Tipo BSON | Rango o valores |
|---|---|---|
| `nombre` | string | p. ej. `Metformina`, `Citrato de calcio` |
| `categoria` | string | `antidiabetico`, `antihipertensivo`, `suplemento`, `gastroprotector`, `antiobesidad` |
| `dosis_mg` | double | 0.1–3000 (dosis por toma) |
| `tomas_por_dia` | int | 1–4 |
| `duracion_dias` | int | 1–365 |

La dosis diaria es `dosis_mg × tomas_por_dia`, el equivalente a `qty × price` del playbook.

## Equivalencia de Q1 y Q2

Las dos versiones de cada consulta deben devolver exactamente lo mismo. `benchmark_runner/verificar.py`
las ejecuta en ambos motores y compara fila por fila (resultado en `results/verificacion/resumen.json`).

| Detalle | MongoDB | PostgreSQL |
|---|---|---|
| Año local | `$year` con `timezone: "-06:00"` | `AT TIME ZONE INTERVAL '-06:00'` |
| Orden de cadenas | Por bytes (por defecto) | `COLLATE "C"` |
| Tipos numéricos | `double` / `int` de BSON | `::float8` / `::int` (no `::numeric`) |
| IMC faltante en Q2 | Queda al final en orden descendente | `NULLS LAST` |
| Empates en Q2 | `_id` | `id` (mismo orden de bytes que el UUID de BSON) |

