#!/usr/bin/env python3
"""Ejecuta una corrida: una celda (motor × perfil × consulta × concurrencia) en una repetición.

Ciclo (definido en results/README.md):
  1. Recrea el contenedor del motor con los límites del perfil (caché del motor en frío) y limpia la caché
     del sistema con drop_caches.
  2. Lanza N clientes en procesos separados, con una conexión cada uno, en lazo cerrado: cada cliente envía
     la siguiente consulta en cuanto termina la anterior. Todos empiezan a la vez.
  3. Calentamiento, ventana de medición y enfriamiento sin carga.
  4. Escribe la fila de mediciones.csv y la evidencia en runs/<id>/.

Uso:
  python benchmark_runner/runner.py --engine postgres --profile C --query Q1 --concurrency 50 --run 1
Variante de memoria (experimento complementario, solo PostgreSQL): --work-mem 64MB. La fila se registra con
query_id "<consulta>_WM<n>" (p. ej., Q1_WM64) para que no se confunda con la configuración de fábrica.
Opciones de prueba (no producen mediciones válidas): --salida DIR, --calentamiento S, --medicion S,
--enfriamiento S.
"""
import argparse
import csv
import gzip
import json
import multiprocessing as mp
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from consultas import RAIZ, conectar_mongo, conectar_postgres, ejecutar_mongo, ejecutar_postgres

PERFILES = {
    "A": ("PROFILE_A_BASELINE", "4", "4096m"),
    "B": ("PROFILE_B_MEDIUM", "2", "2048m"),
    "C": ("PROFILE_C_SEVERE", "1", "1024m"),
}
CONTENEDOR = {"mongo": "bench-mongo", "postgres": "bench-postgres"}
COLUMNAS = ["timestamp_epoch", "engine", "profile", "query_id", "concurrency", "run_number", "throughput_qps",
            "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "cpu_throttled_seconds", "max_rss_mb",
            "disk_spill", "status"]
TIMEOUT_CONSULTA_S = 300
# Por debajo de este espacio libre en el host se aborta la corrida: el disco de Docker Desktop crece sobre
# el disco del Mac y el volcado a disco de Postgres con 50 clientes puede llenarlo
DISCO_MINIMO_BYTES = 8 * 1024 ** 3
PROMETHEUS = "http://127.0.0.1:9090"


def sh(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check).stdout


def compose(*args, env=None):
    subprocess.run(["docker", "compose", *args], cwd=RAIZ, env=env, check=True, capture_output=True, text=True)


# ---------------------------------------------------------------------------------------------------
# Clientes

def cliente(i, motor, qid, work_mem, listo, inicio, t0_compartido, fin_carga, archivo):
    """Un cliente en lazo cerrado. Escribe una fila por consulta en su propio archivo."""
    if motor == "mongo":
        db = conectar_mongo()
        db.command("ping")

        def ejecutar():
            return ejecutar_mongo(db, qid, TIMEOUT_CONSULTA_S)
    else:
        con = conectar_postgres(TIMEOUT_CONSULTA_S, work_mem)

        def ejecutar():
            return ejecutar_postgres(con, qid)
    listo.wait()
    inicio.wait()
    t0 = t0_compartido.value
    time.sleep(max(0.0, t0 - time.time()))
    with open(archivo, "w", newline="") as f:
        w = csv.writer(f)
        while time.time() < t0 + fin_carga:
            ini = time.time()
            p0 = time.perf_counter()
            try:
                ejecutar()
                resultado = "ok"
            except Exception as e:  # el error se registra y el cliente sigue, como haría una aplicación
                resultado = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
                if motor == "postgres" and con.closed:
                    try:
                        con = conectar_postgres(TIMEOUT_CONSULTA_S, work_mem)
                    except Exception:
                        time.sleep(1)
            lat = (time.perf_counter() - p0) * 1000
            w.writerow([i, round(ini * 1000, 3), round((ini * 1000) + lat, 3), round(lat, 3), resultado])
            f.flush()


# ---------------------------------------------------------------------------------------------------
# Contenedor y contadores

def preparar_motor(motor, perfil):
    _, cpus, mem = PERFILES[perfil]
    otro = "postgres" if motor == "mongo" else "mongo"
    env = {**__import__("os").environ, "DB_CPUS": cpus, "DB_MEM": mem}
    compose("up", "-d", "--wait", "cadvisor", "node-exporter", "prometheus", env=env)
    subprocess.run(["docker", "stop", CONTENEDOR[otro]], capture_output=True)
    compose("--profile", motor, "up", "-d", "--wait", "--force-recreate", motor, env=env)
    sh("docker", "run", "--rm", "--privileged", "alpine:3.20", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches")


def leer_cgroup(motor):
    c = CONTENEDOR[motor]
    salida = sh("docker", "exec", c, "sh", "-c", "cat /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.events",
                check=False)
    valores = {}
    for linea in salida.splitlines():
        partes = linea.split()
        if len(partes) == 2 and partes[1].isdigit():
            valores[partes[0]] = int(partes[1])
    return valores


def leer_spill(motor):
    if motor == "mongo":
        m = conectar_mongo().client.admin.command("serverStatus")["metrics"]["query"]
        return {"sort_spillToDisk": int(m["sort"]["spillToDisk"]), "group_spills": int(m["group"]["spills"]),
                "sort_spillToDiskBytes": int(m["sort"]["spillToDiskBytes"]),
                "group_spilledBytes": int(m["group"]["spilledBytes"])}
    with conectar_postgres() as con:
        f, b = con.execute("SELECT temp_files, temp_bytes FROM pg_stat_database WHERE datname = 'bariatria'").fetchone()
        return {"temp_files": int(f), "temp_bytes": int(b)}


def leer_contadores(motor):
    lectura = {"t": time.time(), "cgroup": leer_cgroup(motor)}
    try:
        lectura["spill"] = leer_spill(motor)
    except Exception as e:
        lectura["spill_error"] = f"{type(e).__name__}: {e}"
    return lectura


def config_motor(motor):
    if motor == "mongo":
        db = conectar_mongo()
        cache = db.command("serverStatus")["wiredTiger"]["cache"]["maximum bytes configured"]
        return {"version": db.command("buildInfo")["version"], "wiredtiger_cache_bytes": int(cache)}
    with conectar_postgres() as con:
        filas = con.execute("SELECT name, setting, unit FROM pg_settings WHERE name IN ('server_version', "
                            "'shared_buffers', 'work_mem', 'max_parallel_workers_per_gather', "
                            "'max_parallel_workers', 'temp_file_limit', 'max_connections')").fetchall()
        return {n: (s if not u else f"{s} {u}") for n, s, u in filas}


def inspeccionar(motor):
    info = json.loads(sh("docker", "inspect", CONTENEDOR[motor]))[0]
    hc, estado = info["HostConfig"], info["State"]
    imagen = json.loads(sh("docker", "image", "inspect", info["Image"]))[0]
    return {"id": info["Id"], "imagen": info["Config"]["Image"], "digest": (imagen.get("RepoDigests") or [None])[0],
            "nano_cpus": hc["NanoCpus"], "memory": hc["Memory"], "memory_swap": hc["MemorySwap"],
            "running": estado["Running"], "oom_killed": estado["OOMKilled"], "exit_code": estado["ExitCode"],
            "started_at": estado["StartedAt"]}


# ---------------------------------------------------------------------------------------------------
# Prometheus (cAdvisor)

def prom(ruta, **params):
    url = f"{PROMETHEUS}/api/v1/{ruta}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)["data"]["result"]


def series(contenedor_id, ini, fin):
    # Se filtra por el ID del contenedor, no por su nombre: tras recrearlo, cAdvisor sigue publicando varios
    # minutos la serie del contenedor anterior con el mismo nombre, y se mezclaría con la de esta corrida
    sel = f'id="/docker/{contenedor_id}"'
    consultas = {
        "cpu_uso_nucleos": f'rate(container_cpu_usage_seconds_total{{{sel}}}[5s])',
        "cpu_throttled_s": f'container_cpu_cfs_throttled_seconds_total{{{sel}}}',
        "rss_bytes": f'container_memory_rss{{{sel}}}',
        "working_set_bytes": f'container_memory_working_set_bytes{{{sel}}}',
    }
    datos = {}
    for clave, q in consultas.items():
        for s in prom("query_range", query=q, start=ini, end=fin, step="1s"):
            for t, v in s["values"]:
                datos.setdefault(float(t), {})[clave] = float(v)
    return datos


# ---------------------------------------------------------------------------------------------------
# Corrida

def percentil(valores, p):
    """Rango más cercano: el menor valor con al menos p % de las muestras en o por debajo."""
    if not valores:
        return None
    orden = sorted(valores)
    k = max(1, -(-len(orden) * p // 100))
    return orden[int(k) - 1]


def correr(a):
    perfil_id = PERFILES[a.profile][0]
    query_id = f"{a.query}_WM{int(a.work_mem.upper().removesuffix('MB'))}" if a.work_mem else a.query
    run_id = f"{a.engine}_{a.profile}_{query_id}_c{a.concurrency}_r{a.run}"
    salida = Path(a.salida) if a.salida else RAIZ / "results"
    carpeta = salida / "runs" / run_id
    if carpeta.exists():
        shutil.rmtree(carpeta)
    (carpeta / "clientes").mkdir(parents=True)
    meta = {"id": run_id, "engine": a.engine, "profile": perfil_id, "query_id": query_id, "consulta": a.query,
            "variante_work_mem": a.work_mem,
            "concurrency": a.concurrency, "run_number": a.run, "carga": "lazo cerrado",
            "duraciones_s": {"calentamiento": a.calentamiento, "medicion": a.medicion,
                             "enfriamiento": a.enfriamiento},
            "timeout_consulta_s": TIMEOUT_CONSULTA_S, "fases": {}}
    print(f"[{run_id}] preparando motor y limpiando caché", flush=True)
    meta["fases"]["preparacion_inicio"] = time.time()
    preparar_motor(a.engine, a.profile)
    meta["contenedor_inicio"] = inspeccionar(a.engine)
    meta["config_motor"] = config_motor(a.engine)
    meta["fases"]["preparacion_fin"] = time.time()

    # Vigilancia del disco del host durante toda la corrida
    disco = {"min_libre_bytes": shutil.disk_usage("/").free, "abortado": False}
    detener = threading.Event()

    def vigilar():
        while not detener.wait(2):
            libre = shutil.disk_usage("/").free
            disco["min_libre_bytes"] = min(disco["min_libre_bytes"], libre)
            if libre < DISCO_MINIMO_BYTES and not disco["abortado"]:
                disco["abortado"] = True
                subprocess.run(["docker", "kill", CONTENEDOR[a.engine]], capture_output=True)
    threading.Thread(target=vigilar, daemon=True).start()

    ctx = mp.get_context("spawn")
    listo, inicio = ctx.Barrier(a.concurrency + 1), ctx.Event()
    t0_compartido = ctx.Value("d", 0.0)
    fin_carga = a.calentamiento + a.medicion
    procesos = [ctx.Process(target=cliente, args=(i, a.engine, a.query, a.work_mem, listo, inicio, t0_compartido,
                                                  fin_carga, carpeta / "clientes" / f"{i}.csv"))
                for i in range(a.concurrency)]
    for p in procesos:
        p.start()
    listo.wait()
    t0 = time.time() + 1
    t0_compartido.value = t0
    inicio.set()
    ventana_ini, ventana_fin = t0 + a.calentamiento, t0 + fin_carga
    meta["fases"].update(carga_inicio=t0, ventana_inicio=ventana_ini, ventana_fin=ventana_fin)
    print(f"[{run_id}] {a.concurrency} clientes; calentamiento {a.calentamiento} s, medición {a.medicion} s",
          flush=True)

    time.sleep(max(0.0, ventana_ini - time.time()))
    meta["contadores_inicio"] = leer_contadores(a.engine)
    time.sleep(max(0.0, ventana_fin - time.time()))
    meta["contadores_fin"] = leer_contadores(a.engine)

    # Los clientes no envían más consultas; se espera a que terminen la que tengan en curso
    for p in procesos:
        p.join(TIMEOUT_CONSULTA_S + 60)
        if p.is_alive():
            p.terminate()
    meta["fases"]["carga_fin"] = time.time()
    print(f"[{run_id}] enfriamiento {a.enfriamiento} s", flush=True)
    time.sleep(a.enfriamiento)
    meta["fases"]["enfriamiento_fin"] = time.time()
    detener.set()
    meta["disco_host"] = disco
    meta["contenedor_fin"] = inspeccionar(a.engine)

    # Latencias de todos los clientes
    registros = []
    for archivo in sorted((carpeta / "clientes").glob("*.csv"), key=lambda x: int(x.stem)):
        with open(archivo, newline="") as f:
            for cli, ini, fin, lat, res in csv.reader(f):
                fin_s = float(fin) / 1000
                registros.append((int(cli), float(ini), float(fin), float(lat), res,
                                  ventana_ini <= fin_s < ventana_fin))
    shutil.rmtree(carpeta / "clientes")
    with gzip.open(carpeta / "latencias.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cliente", "inicio_ms", "fin_ms", "latencia_ms", "resultado", "en_ventana"])
        w.writerows(registros)

    en_ventana = [r for r in registros if r[5]]
    exitosas = [r[3] for r in en_ventana if r[4] == "ok"]
    fallidas = [r for r in registros if r[4] != "ok"]
    errores = {}
    for r in fallidas:
        errores[r[4]] = errores.get(r[4], 0) + 1
    meta["consultas"] = {"total": len(registros), "en_ventana": len(en_ventana), "exitosas_en_ventana": len(exitosas),
                         "fallidas_total": len(fallidas), "errores": errores}

    # Throttling, spill y OOM: diferencias entre el inicio y el fin de la ventana
    ci, cf = meta["contadores_inicio"], meta["contadores_fin"]
    throttled = None
    if "throttled_usec" in ci["cgroup"] and "throttled_usec" in cf["cgroup"]:
        throttled = (cf["cgroup"]["throttled_usec"] - ci["cgroup"]["throttled_usec"]) / 1e6
    # Sin las dos lecturas (p. ej., el motor estaba en recuperación tras un OOM) no se puede afirmar nada:
    # disk_spill queda vacío en lugar de false. Tras una caída, Postgres además reinicia sus estadísticas
    spill = None
    if "spill" in ci and "spill" in cf:
        claves = ("sort_spillToDisk", "group_spills") if a.engine == "mongo" else ("temp_files",)
        spill = any(cf["spill"][k] > ci["spill"][k] for k in claves)
    oom_kills = cf["cgroup"].get("oom_kill", 0) - ci["cgroup"].get("oom_kill", 0) if cf["cgroup"] else 0
    oom = meta["contenedor_fin"]["oom_killed"] or oom_kills > 0
    meta["oom_kill_eventos"] = oom_kills

    # RSS y series de cAdvisor
    max_rss = None
    try:
        datos = series(meta["contenedor_inicio"]["id"], meta["fases"]["preparacion_fin"],
                       meta["fases"]["enfriamiento_fin"])
        with gzip.open(carpeta / "metricas.csv.gz", "wt", newline="") as f:
            w = csv.writer(f)
            claves = ["cpu_uso_nucleos", "cpu_throttled_s", "rss_bytes", "working_set_bytes"]
            w.writerow(["t", *claves])
            for t in sorted(datos):
                w.writerow([t, *[datos[t].get(k, "") for k in claves]])
        rss = [v["rss_bytes"] for t, v in datos.items() if ventana_ini <= t <= ventana_fin and "rss_bytes" in v]
        ws = [v["working_set_bytes"] for t, v in datos.items()
              if ventana_ini <= t <= ventana_fin and "working_set_bytes" in v]
        max_rss = round(max(rss) / 1024 ** 2, 1) if rss else None
        meta["max_working_set_mb"] = round(max(ws) / 1024 ** 2, 1) if ws else None
    except Exception as e:
        meta["prometheus_error"] = f"{type(e).__name__}: {e}"

    # Log del motor durante la corrida (evidencia de spill y OOM)
    desde = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(meta["fases"]["preparacion_fin"])) + "Z"
    log = subprocess.run(["docker", "logs", "--since", desde, CONTENEDOR[a.engine]], capture_output=True, text=True)
    with gzip.open(carpeta / "motor.log.gz", "wt") as f:
        f.write(log.stdout + log.stderr)

    if oom:
        status = "oom"
    elif disco["abortado"] or fallidas or not exitosas:
        status = "error"
    elif spill:
        status = "spill"
    else:
        status = "ok"
    if disco["abortado"]:
        meta["motivo_error"] = "espacio libre del host por debajo del mínimo; se detuvo el motor"

    fila = {"timestamp_epoch": int(ventana_ini), "engine": a.engine, "profile": perfil_id, "query_id": query_id,
            "concurrency": a.concurrency, "run_number": a.run,
            "throughput_qps": round(len(exitosas) / a.medicion, 4),
            "latency_p50_ms": round(percentil(exitosas, 50), 3) if exitosas else "",
            "latency_p95_ms": round(percentil(exitosas, 95), 3) if exitosas else "",
            "latency_p99_ms": round(percentil(exitosas, 99), 3) if exitosas else "",
            "cpu_throttled_seconds": round(throttled, 3) if throttled is not None else "",
            "max_rss_mb": max_rss if max_rss is not None else "",
            "disk_spill": {True: "true", False: "false", None: ""}[spill], "status": status}
    meta["fila_csv"] = fila
    (carpeta / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")

    # Repetir una corrida reemplaza su fila en lugar de duplicarla
    csv_path = salida / "mediciones.csv"
    llave = ("engine", "profile", "query_id", "concurrency", "run_number")
    filas = []
    if csv_path.exists():
        with open(csv_path, newline="") as f:
            filas = [r for r in csv.DictReader(f) if tuple(r[k] for k in llave) != tuple(str(fila[k]) for k in llave)]
    filas.append(fila)
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS)
        w.writeheader()
        w.writerows(filas)
    print(f"[{run_id}] {status}: {len(exitosas)} consultas en la ventana, QPS {fila['throughput_qps']}, "
          f"P99 {fila['latency_p99_ms']} ms, throttling {fila['cpu_throttled_seconds']} s, "
          f"RSS máx {fila['max_rss_mb']} MiB, spill {fila['disk_spill']}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=["mongo", "postgres"], required=True)
    ap.add_argument("--profile", choices=list(PERFILES), required=True)
    ap.add_argument("--query", choices=["Q1", "Q2"], required=True)
    ap.add_argument("--concurrency", type=int, required=True)
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--work-mem", help="Variante de memoria de PostgreSQL por sesión, p. ej. 64MB")
    ap.add_argument("--salida", help="Carpeta de salida en lugar de results/ (pruebas y piloto)")
    ap.add_argument("--calentamiento", type=float, default=60)
    ap.add_argument("--medicion", type=float, default=300)
    ap.add_argument("--enfriamiento", type=float, default=60)
    a = ap.parse_args()
    if a.work_mem and (a.engine != "postgres" or not re.fullmatch(r"\d+MB", a.work_mem, re.IGNORECASE)):
        ap.error("--work-mem solo aplica a postgres y se expresa en MB, p. ej. 64MB")
    correr(a)


if __name__ == "__main__":
    main()
