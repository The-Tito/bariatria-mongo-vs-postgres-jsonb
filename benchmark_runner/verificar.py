#!/usr/bin/env python3
"""Verifica que Q1 y Q2 devuelven los mismos resultados en ambos motores y guarda sus planes de ejecución.

Uso (un motor a la vez, como en las mediciones):
  python benchmark_runner/verificar.py ejecutar mongo       Resultados y explain de MongoDB
  python benchmark_runner/verificar.py ejecutar postgres    Resultados y EXPLAIN de PostgreSQL
  python benchmark_runner/verificar.py comparar             Compara y escribe results/verificacion/resumen.json
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
    elif args == ["comparar"]:
        comparar()
    else:
        sys.exit(__doc__)
