#!/usr/bin/env python3
"""Dataset sintético de visitas de la clínica bariátrica y su carga en MongoDB y PostgreSQL.

Uso:
  python synthetic_data_generator.py generate         Genera data/visitas.ndjson.gz y results/dataset_manifest.json
  python synthetic_data_generator.py load mongo       Carga el archivo en MongoDB (validador + índices)
  python synthetic_data_generator.py load postgres    Carga el mismo archivo en PostgreSQL (tabla JSONB + índices)

El archivo guarda cada visita tal como va en el JSONB de PostgreSQL (fechas ISO 8601 UTC, UUID y ObjectId
como texto). Para MongoDB solo se convierten _id, paciente_id, fecha y cerrada_en a sus tipos BSON.
La generación es determinista: misma semilla, mismo archivo byte a byte (ver sha256 en el manifiesto).
"""
import bisect
import gzip
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
ARCHIVO = RAIZ / "data" / "visitas.ndjson.gz"
MANIFIESTO = RAIZ / "results" / "dataset_manifest.json"

# Parámetros del dataset (docs: decisiones de diseño, sección 4)
SEMILLA = 20261028
N_PACIENTES = 10_000
N_VISITAS = 500_000
FECHA_FIN = date(2026, 10, 28)          # último día de datos (hora local); fija para reproducir
ANIOS = 15
CRECIMIENTO_ANUAL = 0.10                # la clínica atiende ~10 % más pacientes nuevos cada año
P_PLIEGUES, P_SIGNOS, P_BIOIMPEDANCIA = 0.60, 0.90, 0.50
P_SIN_ESTATURA = 0.02                   # sin estatura no hay IMC (RN-10)
P_CIERRE_AUTOMATICO = 0.05              # el médico olvidó cerrar la visita (ADR-13)
LOCAL = timezone(timedelta(hours=-6))   # Ciudad de México; mismo desfase fijo que Q1
UTC = timezone.utc
INICIO = FECHA_FIN - timedelta(days=365 * ANIOS)
FIN_LOCAL = datetime.combine(FECHA_FIN, datetime.max.time(), LOCAL)

# Umbral de la mediana de seguimiento antes del ajuste; la búsqueda binaria lo escala para llegar a N_VISITAS
SEGUIMIENTO_MEDIANO_DIAS = 600

NOTAS = [
    "Refiere buena tolerancia a la dieta.",
    "Bajó de peso respecto a la consulta anterior.",
    "Refiere ansiedad por la comida en las noches.",
    "Se refuerza la importancia de la hidratación.",
    "Inició actividad física tres veces por semana.",
    "Presenta estreñimiento leve; se ajusta la fibra.",
    "Se revisan resultados de laboratorio sin cambios relevantes.",
    "Refiere mejor calidad de sueño.",
    "Se observa estancamiento de peso; se ajusta el plan.",
    "Acude acompañada de un familiar.",
    "Refiere dolor articular en rodillas al caminar.",
    "Se explica el registro diario de alimentos.",
    "Se pospone el ajuste de medicamento hasta la siguiente consulta.",
    "Refiere menor consumo de bebidas azucaradas.",
    "Se programa control de glucosa en ayuno.",
]
DIETAS = [
    ("Hipocalórica 1,200 kcal", "5 tiempos de comida; evitar azúcares simples; 2 L de agua al día."),
    ("Hipocalórica 1,400 kcal", "5 tiempos de comida; porciones medidas; proteína en cada tiempo."),
    ("Hipocalórica 1,600 kcal", "4 tiempos de comida; verduras a libre demanda; sin bebidas azucaradas."),
    ("Dieta líquida progresiva", "Líquidos claros 3 días, después licuados proteicos; sorbos pequeños."),
    ("Mediterránea baja en sodio", "Aceite de oliva, pescado dos veces por semana; menos de 2 g de sodio."),
]
MEDICAMENTOS = [
    ("Metformina", "850 mg", "cada 12 h"),
    ("Orlistat", "120 mg", "con cada comida"),
    ("Liraglutida", "3 mg", "cada 24 h"),
    ("Semaglutida", "1 mg", "cada semana"),
    ("Losartán", "50 mg", "cada 24 h"),
    ("Omeprazol", "20 mg", "cada 24 h"),
    ("Multivitamínico", "1 tableta", "cada 24 h"),
    ("Citrato de calcio", "500 mg", "cada 12 h"),
]
DURACIONES = ["15 días", "30 días", "60 días", "90 días"]


def rng(*partes):
    """Generador independiente por entidad: cambiar un paciente no altera a los demás."""
    return random.Random(":".join(str(p) for p in (SEMILLA, *partes)))


def acotar(x, lo, hi):
    # Siempre float: si devolviera el límite entero, round(70, 1) seguiría siendo int y el validador
    # lo rechazaría donde el contrato exige double. Los campos enteros se convierten con int().
    return float(max(lo, min(hi, x)))


# ---------------------------------------------------------------------------------------------------
# Pacientes y agenda de visitas
# ---------------------------------------------------------------------------------------------------

def crear_paciente(pid):
    r = rng("paciente", pid)
    # Año de alta ponderado por el crecimiento de la clínica
    pesos = [(1 + CRECIMIENTO_ANUAL) ** k for k in range(ANIOS)]
    anio = r.choices(range(ANIOS), weights=pesos)[0]
    alta = INICIO + timedelta(days=anio * 365 + r.randrange(365))
    sexo = "F" if r.random() < 0.70 else "M"
    # Desde 19: con 18.0 exactos, el redondeo de la fecha de nacimiento daba 17 años en la primera visita
    edad_alta = r.uniform(19, 65)
    estatura = acotar(r.gauss(160, 6.5) if sexo == "F" else r.gauss(173, 7), 140, 200)
    imc_inicial = acotar(r.gauss(42, 6), 32, 65)
    categoria = r.choices(["normal", "prediabetes", "diabetes"], weights=[50, 25, 25])[0]
    glucosa_base = {"normal": r.gauss(91, 5),
                    "prediabetes": r.gauss(113, 6),
                    "diabetes": r.gauss(168, 30)}[categoria]
    oid = int(datetime.combine(alta, datetime.min.time(), UTC).timestamp()).to_bytes(4, "big") + r.randbytes(8)
    return {
        "pid": pid,
        "paciente_id": oid.hex(),
        "alta": alta,
        "nacimiento": alta - timedelta(days=int(edad_alta * 365.25)),
        "sexo": sexo,
        "estatura": estatura,
        "peso_inicial": imc_inicial * (estatura / 100) ** 2,
        "perdida_max": acotar(r.gauss(0.25, 0.07), 0.05, 0.40),
        "tau": r.uniform(120, 240),
        "glucosa_base": glucosa_base,
        "mejora_glucosa": r.uniform(0.3, 0.9) if categoria != "normal" else r.uniform(0.0, 0.3),
        "z_seguimiento": r.gauss(0, 1),
    }


def agenda(p):
    """Visitas posibles del paciente (desfase en días y hora local), hasta la fecha fin.

    Semanal los primeros 3 meses, quincenal hasta el año y mensual después. No incluye domingos.
    """
    r = rng("agenda", p["pid"])
    visitas, t = [], 0
    while True:
        dia = p["alta"] + timedelta(days=t)
        if dia.weekday() == 6:
            dia += timedelta(days=1)
        if dia > FECHA_FIN:
            break
        minuto = r.randrange(8 * 60, 19 * 60)
        visitas.append((t, datetime.combine(dia, datetime.min.time(), LOCAL) + timedelta(minutes=minuto)))
        if t < 84:
            t += 7 + r.randint(-1, 1)
        elif t < 365:
            t += 14 + r.randint(-2, 2)
        else:
            t += 30 + r.randint(-5, 5)
    return visitas


def visitas_por_paciente(pacientes, agendas, escala):
    conteos = []
    for p, ag in zip(pacientes, agendas):
        dias = SEGUIMIENTO_MEDIANO_DIAS * escala * math.exp(0.9 * p["z_seguimiento"])
        conteos.append(max(1, bisect.bisect_right([t for t, _ in ag], dias)))
    return conteos


def ajustar_conteos(pacientes, agendas):
    """Escala la duración del seguimiento hasta tener al menos N_VISITAS y recorta el excedente."""
    lo, hi = 0.1, 20.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if sum(visitas_por_paciente(pacientes, agendas, mid)) >= N_VISITAS:
            hi = mid
        else:
            lo = mid
    conteos = visitas_por_paciente(pacientes, agendas, hi)
    exceso = sum(conteos) - N_VISITAS
    # El excedente sale de la última visita de los pacientes con más visitas (abandono un poco antes)
    for pid in sorted(range(len(conteos)), key=lambda i: (-conteos[i], i))[:exceso]:
        conteos[pid] -= 1
    assert sum(conteos) == N_VISITAS
    return conteos, hi


# ---------------------------------------------------------------------------------------------------
# Contenido de una visita
# ---------------------------------------------------------------------------------------------------

def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def perdida(p, t):
    """Fracción de peso perdido a los t días: baja rápido el primer año y recupera un poco después de dos."""
    base = p["perdida_max"] * (1 - math.exp(-t / p["tau"]))
    recuperacion = min(0.08, 0.02 * max(0, t - 730) / 365)
    return max(0.0, base - recuperacion)


def crear_visita(p, k, t, fecha_local):
    r = rng("visita", p["pid"], k)
    edad = int((fecha_local.date() - p["nacimiento"]).days // 365.25)
    hombre = p["sexo"] == "M"
    L = perdida(p, t)
    peso = round(acotar(p["peso_inicial"] * (1 - L) + r.gauss(0, 0.6), 35, 390), 1)
    imc_real = peso / (p["estatura"] / 100) ** 2

    # Antropometría (siempre). Sin estatura no se calcula el IMC.
    cintura = round(acotar((1.0 if hombre else 0.9) * (imc_real * 2.6 + 15) + r.gauss(0, 3), 50, 240), 1)
    cadera = round(acotar((imc_real * 1.9 + 50 if hombre else imc_real * 2.2 + 45) + r.gauss(0, 3), 60, 240), 1)
    medidas = {"peso_kg": peso}
    calculos = {}
    if r.random() >= P_SIN_ESTATURA:
        estatura = round(p["estatura"] + r.gauss(0, 0.4), 1)
        medidas["estatura_cm"] = estatura
        calculos["imc"] = round(peso / (estatura / 100) ** 2, 2)
    medidas["circunferencias_cm"] = {
        "cintura": cintura,
        "cadera": cadera,
        "muneca": round(acotar((18.5 if hombre else 16.5) + (imc_real - 30) * 0.08 + r.gauss(0, 0.4), 12, 28), 1),
        "brazo": round(acotar(imc_real * 0.8 + 12 + r.gauss(0, 1.5), 18, 75), 1),
    }
    calculos["indice_cintura_cadera"] = round(cintura / cadera, 2)
    estudios = [{"tipo_estudio": "antropometria", "mediciones": medidas, "resultados_calculados": calculos}]

    if r.random() < P_PLIEGUES:
        triceps = acotar((imc_real - 18) * (0.8 if hombre else 1.1) + 8 + r.gauss(0, 2), 2, 75)
        estudios.append({"tipo_estudio": "pliegues_cutaneos", "mediciones": {"pliegues_mm": {
            "biceps": round(acotar(triceps * 0.55 + r.gauss(0, 1.5), 1, 80), 1),
            "triceps": round(triceps, 1),
            "subescapular": round(acotar(triceps * 1.05 + r.gauss(0, 2), 1, 80), 1),
            "suprailiaco": round(acotar(triceps * 0.95 + r.gauss(0, 2), 1, 80), 1),
        }}})

    if r.random() < P_SIGNOS:
        g0 = p["glucosa_base"]
        mejora = p["mejora_glucosa"] * min(1.0, L / 0.25)
        media = g0 - (g0 - 90) * mejora if g0 > 90 else g0
        ruido = 6 if g0 < 100 else (9 if g0 < 126 else 18)
        sistolica = int(round(acotar(105 + 0.9 * (imc_real - 25) + 0.15 * (edad - 40) + r.gauss(0, 9), 80, 220)))
        estudios.append({"tipo_estudio": "signos_vitales", "mediciones": {
            "glucosa_mg_dl": int(round(acotar(media + r.gauss(0, ruido), 55, 450))),
            "presion": {"sistolica": sistolica,
                        "diastolica": int(round(acotar(0.6 * sistolica + 5 + r.gauss(0, 5), 45, 140)))},
        }})

    if r.random() < P_BIOIMPEDANCIA:
        pct = round(acotar(1.2 * imc_real + 0.23 * edad - (10.8 if hombre else 0) - 5.4 + r.gauss(0, 1.5), 5, 70), 1)
        estudios.append({"tipo_estudio": "bioimpedancia", "mediciones": {
            "pct_grasa": pct,
            "masa_muscular_kg": round(peso * (1 - pct / 100) * 0.52, 1),
            "grasa_visceral": int(acotar(round((imc_real - 15) * 0.55 + (2 if hombre else 0) + r.gauss(0, 1)), 1, 59)),
            "edad_metabolica": int(acotar(round(edad + (imc_real - 25) * 0.9 + r.gauss(0, 2)), 12, 99)),
        }})

    resumen = {"peso_kg": peso}
    if "imc" in calculos:
        resumen["imc"] = calculos["imc"]

    visita = {
        "_id": str(uuid.UUID(int=r.getrandbits(128), version=4)),
        "paciente_id": p["paciente_id"],
        "fecha": iso(fecha_local),
        "tipo": "inicial" if k == 0 else "seguimiento",
    }
    if fecha_local.date() == FECHA_FIN:
        visita["estado"] = "abierta"
    else:
        visita["estado"] = "cerrada"
        if r.random() < P_CIERRE_AUTOMATICO:
            cierre = datetime.combine(fecha_local.date(), datetime.max.time().replace(microsecond=0), LOCAL)
        else:
            cierre = fecha_local + timedelta(minutes=r.randint(20, 90))
        visita["cerrada_en"] = iso(cierre)
    visita["datos_paciente"] = {"sexo": p["sexo"], "edad_anios": edad}
    visita["resumen"] = resumen
    visita["estudios"] = estudios
    if r.random() < 0.70:
        visita["notas"] = " ".join(r.sample(NOTAS, r.randint(1, 3)))
    if r.random() < 0.55:
        nombre, indicaciones = r.choice(DIETAS)
        visita["dieta"] = {"nombre": nombre, "indicaciones": indicaciones}
    if r.random() < 0.45:
        visita["medicamentos"] = [
            {"nombre": n, "dosis": d, "frecuencia": f, "duracion": r.choice(DURACIONES)}
            for n, d, f in r.sample(MEDICAMENTOS, r.randint(1, 3))
        ]
    return visita


# ---------------------------------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------------------------------

def percentiles(valores, ps=(50, 95, 99)):
    v = sorted(valores)
    return {f"p{p}": v[min(len(v) - 1, int(len(v) * p / 100))] for p in ps} | {"max": v[-1]}


def generar():
    import bson  # de pymongo: mide el tamaño BSON real de cada documento

    t0 = time.time()
    pacientes = [crear_paciente(i) for i in range(N_PACIENTES)]
    agendas = [agenda(p) for p in pacientes]
    conteos, escala = ajustar_conteos(pacientes, agendas)

    # Orden cronológico de inserción, como en la clínica (igual para ambos motores)
    orden = sorted((fecha, p["pid"], k, t)
                   for p, ag, n in zip(pacientes, agendas, conteos)
                   for k, (t, fecha) in enumerate(ag[:n]))

    ARCHIVO.parent.mkdir(exist_ok=True)
    sha = hashlib.sha256()
    tam_json, tam_bson = [], []
    por_anio, estudios_tipo, n_estudios = Counter(), Counter(), Counter()
    glucosas = altas = sin_imc = abiertas = cierre_auto = 0
    periodo_q2 = 0
    q2_ini, q2_fin = "2026-01-01T06:00:00.000Z", "2027-01-01T06:00:00.000Z"

    with gzip.GzipFile(ARCHIVO, "wb", mtime=0) as gz:
        for fecha, pid, k, t in orden:
            v = crear_visita(pacientes[pid], k, t, fecha)
            linea = json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            gz.write(linea)
            sha.update(linea)
            tam_json.append(len(linea) - 1)
            tam_bson.append(len(bson.encode(a_bson(v))))
            por_anio[fecha.year] += 1
            n_estudios[len(v["estudios"])] += 1
            for e in v["estudios"]:
                estudios_tipo[e["tipo_estudio"]] += 1
                if e["tipo_estudio"] == "signos_vitales":
                    glucosas += 1
                    altas += e["mediciones"]["glucosa_mg_dl"] >= 100
            sin_imc += "imc" not in v["resumen"]
            abiertas += v["estado"] == "abierta"
            cierre_auto += v.get("cerrada_en", "").endswith("05:59:59.000Z")
            periodo_q2 += v["estado"] == "cerrada" and q2_ini <= v["fecha"] < q2_fin

    vpp = sorted(conteos)
    manifiesto = {
        "archivo": str(ARCHIVO.relative_to(RAIZ)),
        "sha256_sin_comprimir": sha.hexdigest(),
        "parametros": {
            "semilla": SEMILLA, "pacientes": N_PACIENTES, "visitas": N_VISITAS,
            "fecha_fin_local": FECHA_FIN.isoformat(), "anios": ANIOS,
            "crecimiento_anual": CRECIMIENTO_ANUAL,
            "p_pliegues": P_PLIEGUES, "p_signos_vitales": P_SIGNOS, "p_bioimpedancia": P_BIOIMPEDANCIA,
            "p_sin_estatura": P_SIN_ESTATURA, "p_cierre_automatico": P_CIERRE_AUTOMATICO,
            "escala_seguimiento": round(escala, 4),
        },
        "resultado": {
            "visitas": len(orden),
            "pacientes_con_visitas": sum(1 for c in conteos if c > 0),
            "visitas_por_paciente": {"min": vpp[0], "mediana": statistics.median(vpp), "max": vpp[-1],
                                     "promedio": round(sum(vpp) / len(vpp), 1)},
            "visitas_por_anio": dict(sorted(por_anio.items())),
            "estudios_por_visita": {"promedio": round(sum(k * n for k, n in n_estudios.items()) / len(orden), 2),
                                    "distribucion": dict(sorted(n_estudios.items()))},
            "pct_visitas_con_estudio": {k: round(100 * n / len(orden), 1) for k, n in estudios_tipo.items()},
            "pct_glucosa_ge_100": round(100 * altas / glucosas, 1),
            "pct_sin_imc": round(100 * sin_imc / len(orden), 2),
            "visitas_abiertas": abiertas,
            "pct_cierre_automatico": round(100 * cierre_auto / (len(orden) - abiertas), 1),
            "q2_visitas_cerradas_2026": periodo_q2,
            "bytes_json": percentiles(tam_json) | {"promedio": round(sum(tam_json) / len(tam_json))},
            "bytes_bson": percentiles(tam_bson) | {"promedio": round(sum(tam_bson) / len(tam_bson))},
            "pct_bson_mayor_2kb": round(100 * sum(b > 2032 for b in tam_bson) / len(tam_bson), 2),
        },
        "segundos": round(time.time() - t0, 1),
    }
    MANIFIESTO.parent.mkdir(exist_ok=True)
    MANIFIESTO.write_text(json.dumps(manifiesto, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifiesto["resultado"], ensure_ascii=False, indent=2))
    print(f"{ARCHIVO.relative_to(RAIZ)}  sha256={manifiesto['sha256_sin_comprimir']}  ({manifiesto['segundos']} s)")


# ---------------------------------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------------------------------

def leer_visitas():
    if not ARCHIVO.exists():
        sys.exit(f"No existe {ARCHIVO.relative_to(RAIZ)}: ejecuta primero 'generate'.")
    with gzip.open(ARCHIVO, "rt", encoding="utf-8") as f:
        for linea in f:
            yield linea.rstrip("\n")


def a_fecha(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def a_bson(v):
    """Representación JSONB → tipos BSON. Solo cambian los cuatro campos que JSON no puede expresar."""
    from bson import Binary, ObjectId

    d = dict(v)
    d["_id"] = Binary.from_uuid(uuid.UUID(v["_id"]))
    d["paciente_id"] = ObjectId(v["paciente_id"])
    d["fecha"] = a_fecha(v["fecha"])
    if "cerrada_en" in v:
        d["cerrada_en"] = a_fecha(v["cerrada_en"])
    return d


def motivo_rechazo(detalle, ruta=""):
    """Resume el detalle de $jsonSchema como 'ruta.del.campo: motivo'."""
    for regla in detalle.get("schemaRulesNotSatisfied", []) + detalle.get("details", []):
        for prop in regla.get("propertiesNotSatisfied", []):
            for sub in prop.get("details", []):
                return motivo_rechazo(sub, f"{ruta}.{prop['propertyName']}".lstrip("."))
        if "reason" in regla:
            return f"{ruta}: {regla['reason']} ({regla.get('consideredValue', '')})"
        if regla.get("details") or regla.get("schemaRulesNotSatisfied"):
            return motivo_rechazo(regla, ruta)
    return f"{ruta}: {detalle.get('reason', detalle.get('operatorName', '?'))}"


def cargar_mongo():
    from pymongo import MongoClient
    from pymongo.errors import BulkWriteError

    uri = os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27018")
    db = MongoClient(uri)[os.environ.get("MONGO_DB", "bariatria")]
    esquema = json.loads((RAIZ / "queries/mongo/visitas.schema.json").read_text())
    indices = json.loads((RAIZ / "queries/mongo/indexes.json").read_text())

    t0 = time.time()
    db.drop_collection("visitas")
    db.create_collection("visitas", validator={"$jsonSchema": esquema},
                         validationLevel="strict", validationAction="error")
    rechazos = Counter()

    def insertar(lote):
        # Sigue con los demás lotes para reportar todos los rechazos del validador en una sola pasada
        try:
            db.visitas.insert_many(lote, ordered=False)
        except BulkWriteError as e:
            for err in e.details["writeErrors"]:
                rechazos[motivo_rechazo(err.get("errInfo", {}).get("details", {}))] += 1

    lote = []
    for linea in leer_visitas():
        lote.append(a_bson(json.loads(linea)))
        if len(lote) == 5000:
            insertar(lote)
            lote = []
    if lote:
        insertar(lote)
    if rechazos:
        sys.exit("El validador rechazó documentos:\n" +
                 "\n".join(f"  {n:>7}  {m}" for m, n in rechazos.most_common()))
    t_carga = time.time() - t0
    db.command({"createIndexes": "visitas", "indexes": indices})
    t_total = time.time() - t0

    s = next(db.visitas.aggregate([{"$collStats": {"storageStats": {}}}]))["storageStats"]
    estado = {
        "motor": "mongo", "version": db.command("buildInfo")["version"],
        "documentos": db.visitas.count_documents({}),
        "segundos_insercion": round(t_carga, 1), "segundos_total": round(t_total, 1),
        "bytes_datos_sin_comprimir": s["size"], "bytes_promedio_documento": s["avgObjSize"],
        "bytes_almacenamiento": s["storageSize"], "bytes_indices": s["indexSizes"],
    }
    guardar_estado(estado)


def cargar_postgres():
    import psycopg

    dsn = os.environ.get("PG_DSN", "postgresql://bench:bench@127.0.0.1:5433/bariatria")
    with psycopg.connect(dsn, autocommit=True) as con:
        t0 = time.time()
        con.execute("DROP TABLE IF EXISTS visitas")
        ejecutar_sql(con, RAIZ / "queries/postgres/schema.sql")
        with con.cursor() as cur, cur.copy("COPY visitas (id, data) FROM STDIN") as copia:
            for linea in leer_visitas():
                copia.write_row((json.loads(linea)["_id"], linea))
        t_carga = time.time() - t0
        ejecutar_sql(con, RAIZ / "queries/postgres/indexes.sql")
        t_total = time.time() - t0

        fila = con.execute("""
            SELECT count(*),
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY pg_column_size(data)),
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY pg_column_size(data)),
                   max(pg_column_size(data)),
                   round(100.0 * count(pg_column_compression(data)) / count(*), 2)
            FROM visitas""").fetchone()
        tam = con.execute("""
            SELECT pg_relation_size('visitas'),
                   coalesce(pg_total_relation_size(reltoastrelid), 0),
                   pg_indexes_size('visitas'), pg_total_relation_size('visitas')
            FROM pg_class WHERE relname = 'visitas'""").fetchone()
        estado = {
            "motor": "postgres", "version": con.execute("SHOW server_version").fetchone()[0],
            "documentos": fila[0],
            "segundos_insercion": round(t_carga, 1), "segundos_total": round(t_total, 1),
            "bytes_columna_data": {"p50": fila[1], "p95": fila[2], "max": fila[3]},
            "pct_filas_comprimidas_toast": float(fila[4]),
            "bytes_tabla": tam[0], "bytes_toast": tam[1], "bytes_indices": tam[2], "bytes_total": tam[3],
        }
    guardar_estado(estado)


def ejecutar_sql(con, ruta):
    """Ejecuta cada sentencia por separado: VACUUM no puede ir dentro de un bloque de transacción."""
    sin_comentarios = "\n".join(l for l in ruta.read_text().splitlines() if not l.strip().startswith("--"))
    for sentencia in filter(None, (s.strip() for s in sin_comentarios.split(";"))):
        con.execute(sentencia)


def guardar_estado(estado):
    assert estado["documentos"] == N_VISITAS, f"se esperaban {N_VISITAS} documentos"
    ruta = RAIZ / "results" / f"carga_{estado['motor']}.json"
    ruta.parent.mkdir(exist_ok=True)
    ruta.write_text(json.dumps(estado, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(estado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["generate"]:
        generar()
    elif args == ["load", "mongo"]:
        cargar_mongo()
    elif args == ["load", "postgres"]:
        cargar_postgres()
    else:
        sys.exit(__doc__)
