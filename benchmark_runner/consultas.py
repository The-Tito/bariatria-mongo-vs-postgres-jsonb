"""Carga y ejecución de Q1 y Q2 en ambos motores.

Lo usan el verificador de equivalencia y el runner, para que lo verificado sea exactamente lo medido.
Cada ejecución consume el resultado completo y lo devuelve en una forma canónica comparable entre motores.
"""
import os
from datetime import timezone
from pathlib import Path

from bson import json_util

RAIZ = Path(__file__).resolve().parent.parent
CONSULTAS = ("Q1", "Q2")
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27018")
MONGO_DB = os.environ.get("MONGO_DB", "bariatria")
PG_DSN = os.environ.get("PG_DSN", "postgresql://bench:bench@127.0.0.1:5433/bariatria")


def pipeline(qid):
    # json_util interpreta {"$date": ...} como fecha BSON
    return json_util.loads((RAIZ / "queries/mongo" / f"{qid.lower()}.json").read_text())


def sql(qid):
    return (RAIZ / "queries/postgres" / f"{qid.lower()}.sql").read_text()


def conectar_mongo():
    from pymongo import MongoClient
    return MongoClient(MONGO_URI, uuidRepresentation="standard")[MONGO_DB]


def conectar_postgres():
    import psycopg
    return psycopg.connect(PG_DSN, autocommit=True)


def ejecutar_mongo(db, qid):
    filas = list(db.visitas.aggregate(pipeline(qid)))
    if qid == "Q1":
        return [(d["_id"]["anio"], d["_id"]["medicamento"], d["prescripciones"],
                 d["dosis_diaria_total_mg"], d["dosis_diaria_prom_mg"]) for d in filas]
    return [(str(d["_id"]), str(d["paciente_id"]),
             d["fecha"].replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
             d["resumen"].get("imc"), d["resumen"]["peso_kg"]) for d in filas]


def ejecutar_postgres(con, qid):
    filas = con.execute(sql(qid)).fetchall()
    if qid == "Q1":
        return [(a, m, n, total, prom) for a, m, n, total, prom in filas]
    return [(str(i), p, f, imc, peso) for i, p, f, imc, peso in filas]
