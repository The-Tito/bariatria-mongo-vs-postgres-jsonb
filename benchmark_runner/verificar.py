#!/usr/bin/env python3
"""Verifica que Q1 y Q2 devuelven los mismos resultados en ambos motores y guarda sus planes de ejecución.

Uso (un motor a la vez, como en las mediciones):
  python benchmark_runner/verificar.py ejecutar mongo       Resultados y explain de MongoDB
  python benchmark_runner/verificar.py ejecutar postgres    Resultados y EXPLAIN de PostgreSQL
  python benchmark_runner/verificar.py comparar             Compara y escribe results/verificacion/resumen.json
  python benchmark_runner/verificar.py variantes mongo      Planes de Q1 y Q2 con allowDiskUse false y true
  python benchmark_runner/verificar.py variantes postgres   Planes de Q1 y Q2 con work_mem de 4MB y 64MB
"""
import json
import math
import sys
import time

from consultas import (CONSULTAS, RAIZ, conectar_mongo, conectar_postgres, ejecutar_mongo,
                       ejecutar_postgres, pipeline, sql)

SALIDA = RAIZ / "results" / "verificacion"
EXPLAIN = RAIZ / "results" / "explain"
# Las sumas son exactas (dosis enteras); solo los promedios pueden diferir en el último dígito
TOLERANCIA_RELATIVA = 1e-9


def ejecutar(motor):
    SALIDA.mkdir(parents=True, exist_ok=True)
    EXPLAIN.mkdir(parents=True, exist_ok=True)
    salida = {"motor": motor, "consultas": {}}
    if motor == "mongo":
        db = conectar_mongo()
        salida["version"] = db.command("buildInfo")["version"]
        for qid in CONSULTAS:
            t0 = time.perf_counter()
            filas = ejecutar_mongo(db, qid)
            salida["consultas"][qid] = {"ms": round((time.perf_counter() - t0) * 1000, 1), "filas": filas}
            plan = db.command("explain", {"aggregate": "visitas", "pipeline": pipeline(qid), "cursor": {}},
                              verbosity="executionStats")
            (EXPLAIN / f"mongo_{qid}.json").write_text(json.dumps(json.loads(json_dumps(plan)), indent=2) + "\n")
    else:
        with conectar_postgres() as con:
            salida["version"] = con.execute("SHOW server_version").fetchone()[0]
            for qid in CONSULTAS:
                t0 = time.perf_counter()
                filas = ejecutar_postgres(con, qid)
                salida["consultas"][qid] = {"ms": round((time.perf_counter() - t0) * 1000, 1), "filas": filas}
                consulta = sql(qid).rstrip().rstrip(";")
                texto = con.execute(f"EXPLAIN (ANALYZE, BUFFERS, VERBOSE) {consulta}").fetchall()
                (EXPLAIN / f"postgres_{qid}.txt").write_text("\n".join(r[0] for r in texto) + "\n")
                plan = con.execute(f"EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) {consulta}").fetchone()[0]
                (EXPLAIN / f"postgres_{qid}.json").write_text(json.dumps(plan, indent=2) + "\n")
    (SALIDA / f"{motor}.json").write_text(json.dumps(salida, ensure_ascii=False) + "\n")
    for qid, r in salida["consultas"].items():
        print(f"{motor} {qid}: {len(r['filas'])} filas en {r['ms']} ms")


VARIANTES = {"mongo": [("allowDiskUse_false", False), ("allowDiskUse_true", True)],
             "postgres": [("work_mem_4MB", "4MB"), ("work_mem_64MB", "64MB")]}


def etapas_mongo(plan):
    """Etapas que ordenan o agrupan, con su memoria y si volcaron a disco."""
    salida = []

    def recorrer(o):
        if isinstance(o, dict):
            nombre = o.get("stage") or next((k for k in o if k in ("$group", "$sort")), None)
            if nombre in ("SORT", "GROUP", "$group", "$sort"):
                salida.append({"etapa": nombre, **{k: o[k] for k in ("usedDisk", "spills", "totalDataSizeSorted",
                                                                       "totalDataSizeSortedBytesEstimate", "memLimit",
                                                                       "maxAccumulatorMemoryUsageBytes") if k in o}})
            for v in o.values():
                recorrer(v)
        elif isinstance(o, list):
            for v in o:
                recorrer(v)
    recorrer(plan)
    return salida


def variantes(motor):
    """Plan de ejecución de cada consulta con cada variante de memoria (playbook §5.2)."""
    destino = EXPLAIN / "variantes"
    destino.mkdir(parents=True, exist_ok=True)
    resumen = {}
    if motor == "mongo":
        db = conectar_mongo()
        for qid in CONSULTAS:
            for nombre, permitir in VARIANTES["mongo"]:
                try:
                    plan = db.command("explain", {"aggregate": "visitas", "pipeline": pipeline(qid), "cursor": {},
                                                  "allowDiskUse": permitir}, verbosity="executionStats")
                    plan = json.loads(json_dumps(plan))
                    resumen[f"{qid}_{nombre}"] = {"etapas": etapas_mongo(plan)}
                except Exception as e:  # allowDiskUse:false falla si una etapa excede 100 MB
                    plan = {"error": f"{type(e).__name__}: {e}"}
                    resumen[f"{qid}_{nombre}"] = plan
                (destino / f"mongo_{qid}_{nombre}.json").write_text(json.dumps(plan, indent=2) + "\n")
    else:
        for qid in CONSULTAS:
            for nombre, wm in VARIANTES["postgres"]:
                with conectar_postgres(work_mem=wm) as con:
                    consulta = sql(qid).rstrip().rstrip(";")
                    texto = [r[0] for r in con.execute(f"EXPLAIN (ANALYZE, BUFFERS, VERBOSE) {consulta}").fetchall()]
                (destino / f"postgres_{qid}_{nombre}.txt").write_text("\n".join(texto) + "\n")
                resumen[f"{qid}_{nombre}"] = {"lineas_clave": [l.strip() for l in texto if any(
                    k in l for k in ("Sort Method", "Batches", "Disk:", "Execution Time", "HashAggregate",
                                     "GroupAggregate", "temp read"))]}
    (destino / f"resumen_{motor}.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2) + "\n")
    for k, v in resumen.items():
        print(k, json.dumps(v, ensure_ascii=False)[:300])


def json_dumps(doc):
    from bson import json_util
    return json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS)


def iguales(a, b):
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            return a is b
        return math.isclose(a, b, rel_tol=TOLERANCIA_RELATIVA)
    return a == b


def comparar():
    m = json.loads((SALIDA / "mongo.json").read_text())
    p = json.loads((SALIDA / "postgres.json").read_text())
    resumen = {"mongo": m["version"], "postgres": p["version"],
               "tolerancia_relativa_promedios": TOLERANCIA_RELATIVA, "consultas": {}}
    ok_total = True
    for qid in CONSULTAS:
        fm, fp = m["consultas"][qid]["filas"], p["consultas"][qid]["filas"]
        diferencias = [i for i, (x, y) in enumerate(zip(fm, fp))
                       if len(x) != len(y) or not all(iguales(a, b) for a, b in zip(x, y))]
        ok = len(fm) == len(fp) and not diferencias
        ok_total &= ok
        resumen["consultas"][qid] = {
            "filas_mongo": len(fm), "filas_postgres": len(fp), "equivalentes": ok,
            "primeras_diferencias": [{"fila": i, "mongo": fm[i], "postgres": fp[i]} for i in diferencias[:5]],
            "max_diferencia_relativa": max(
                (abs(a - b) / max(abs(a), abs(b)) for x, y in zip(fm, fp) for a, b in zip(x, y)
                 if isinstance(a, float) and isinstance(b, float) and max(abs(a), abs(b)) > 0), default=0.0),
        }
        print(f"{qid}: mongo {len(fm)} filas, postgres {len(fp)} filas -> "
              f"{'EQUIVALENTES' if ok else 'DIFERENTES'}")
        for d in resumen["consultas"][qid]["primeras_diferencias"]:
            print(f"   fila {d['fila']}: mongo={d['mongo']} postgres={d['postgres']}")
    resumen["equivalentes"] = ok_total
    (SALIDA / "resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2) + "\n")
    sys.exit(0 if ok_total else 1)


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "ejecutar" and args[1] in ("mongo", "postgres"):
        ejecutar(args[1])
    elif len(args) == 2 and args[0] == "variantes" and args[1] in ("mongo", "postgres"):
        variantes(args[1])
    elif args == ["comparar"]:
        comparar()
    else:
        sys.exit(__doc__)
