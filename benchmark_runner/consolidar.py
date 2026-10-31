#!/usr/bin/env python3
"""Resume las mediciones por celda y genera las gráficas del reporte (secciones 3, 4 y M3).

Uso:
  python benchmark_runner/consolidar.py [carpeta]     carpeta = results (por defecto) o results/variantes

Escribe en <carpeta>/resumen/:
  celdas.csv        Una fila por celda: mediana, mínimo y máximo de cada KPI de sus repeticiones, estados,
                    muestras y huella de CPU y memoria por cada 1,000 documentos (M3)
  celdas.md         La misma tabla, resumida, para el reporte
  graficas/*.png    P99 vs concurrencia, throttling vs QPS, RSS máximo y curva de RSS en el tiempo
"""
import csv
import gzip
import json
import statistics
import sys
from pathlib import Path

from consultas import RAIZ

PERFILES = ["PROFILE_A_BASELINE", "PROFILE_B_MEDIUM", "PROFILE_C_SEVERE"]
CORTO = {"PROFILE_A_BASELINE": "A", "PROFILE_B_MEDIUM": "B", "PROFILE_C_SEVERE": "C"}
LIMITE_MIB = {"PROFILE_A_BASELINE": 4096, "PROFILE_B_MEDIUM": 2048, "PROFILE_C_SEVERE": 1024}
CONCURRENCIAS = [1, 10, 50]
MOTORES = ["mongo", "postgres"]
KPIS = ["throughput_qps", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "cpu_throttled_seconds",
        "max_rss_mb"]
# Las latencias de una corrida con OOM o error salen de unas pocas consultas terminadas entre reinicios: no
# representan a la celda. Se resumen solo con repeticiones completas; el QPS sí usa todas (~0 es el dato real)
LATENCIAS = {"latency_p50_ms", "latency_p95_ms", "latency_p99_ms"}
COMPLETAS = {"ok", "spill"}

# Paleta categórica validada (dataviz: slots 1 y 2, CVD ΔE 24.7) y tinta de texto
COLOR = {"mongo": "#2a78d6", "postgres": "#eb6834"}
MARCADOR = {"mongo": "o", "postgres": "s"}
NOMBRE = {"mongo": "MongoDB", "postgres": "PostgreSQL JSONB"}
SUPERFICIE, TINTA, TINTA_2, REJILLA = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3de"


def documentos_por_consulta():
    """Documentos que lee cada consulta: Q1 recorre toda la colección; Q2 lee el rango de fechas por índice."""
    manifiesto = json.loads((RAIZ / "results/dataset_manifest.json").read_text())
    plan_q2 = json.loads((RAIZ / "results/explain/mongo_Q2.json").read_text())
    return {"Q1": manifiesto["resultado"]["visitas"],
            "Q2": plan_q2["executionStats"]["totalDocsExamined"]}


def numero(v):
    return float(v) if v not in ("", None) else None


def resumen(valores):
    valores = [v for v in valores if v is not None]
    if not valores:
        return None, None, None
    return statistics.median(valores), min(valores), max(valores)


def leer_meta(carpeta, fila):
    consulta = fila["query_id"]
    run_id = (f"{fila['engine']}_{CORTO[fila['profile']]}_{consulta}_c{fila['concurrency']}"
              f"_r{fila['run_number']}")
    ruta = carpeta / "runs" / run_id / "meta.json"
    return json.loads(ruta.read_text()) if ruta.exists() else None


def consolidar(carpeta):
    filas = list(csv.DictReader(open(carpeta / "mediciones.csv", newline="")))
    docs = documentos_por_consulta()
    celdas = {}
    for f in filas:
        llave = (f["engine"], f["profile"], f["query_id"], int(f["concurrency"]))
        celdas.setdefault(llave, []).append(f)

    salida = []
    for (motor, perfil, qid, conc), reps in sorted(celdas.items(), key=lambda x: (
            x[0][2], MOTORES.index(x[0][0]), PERFILES.index(x[0][1]), x[0][3])):
        c = {"engine": motor, "profile": perfil, "query_id": qid, "concurrency": conc, "repeticiones": len(reps)}
        completas = [r for r in reps if r["status"] in COMPLETAS]
        c["repeticiones_completas"] = len(completas)
        for k in KPIS:
            med, mn, mx = resumen([numero(r[k]) for r in (completas if k in LATENCIAS else reps)])
            c[f"{k}_mediana"], c[f"{k}_min"], c[f"{k}_max"] = med, mn, mx
        estados = {}
        for r in reps:
            estados[r["status"]] = estados.get(r["status"], 0) + 1
        c["status"] = " ".join(f"{k}×{v}" for k, v in sorted(estados.items()))
        c["disk_spill"] = " ".join(f"{k or 'sin lectura'}×{v}" for k, v in sorted(
            {r["disk_spill"]: sum(x["disk_spill"] == r["disk_spill"] for x in reps) for r in reps}.items()))

        # Muestras, fracción de periodos estrangulados y M3, desde meta.json
        muestras, fraccion, cpu_1000, rss_1000 = [], [], [], []
        base = qid.split("_")[0]
        for r in reps:
            m = leer_meta(carpeta, r)
            if not m:
                continue
            ok = m["consultas"]["exitosas_en_ventana"]
            muestras.append(ok)
            g0, g1 = m["contadores_inicio"]["cgroup"], m["contadores_fin"]["cgroup"]
            if g1 and "nr_periods" in g0 and "nr_periods" in g1 and g1["nr_periods"] > g0["nr_periods"]:
                fraccion.append((g1["nr_throttled"] - g0["nr_throttled"]) / (g1["nr_periods"] - g0["nr_periods"]))
            if r["status"] not in COMPLETAS:  # M3 solo con repeticiones completas, igual que las latencias
                continue
            if ok and "usage_usec" in g0 and "usage_usec" in g1:
                cpu_s = (g1["usage_usec"] - g0["usage_usec"]) / 1e6
                cpu_1000.append(cpu_s * 1000 / (ok * docs[base] / 1000))
            rss = numero(r["max_rss_mb"])
            if ok and rss is not None:
                rss_1000.append(rss / (conc * docs[base] / 1000))
        c["muestras_min"] = min(muestras) if muestras else None
        c["fraccion_periodos_estrangulados_mediana"] = resumen(fraccion)[0]
        c["cpu_ms_por_1000_docs_mediana"] = resumen(cpu_1000)[0]
        c["rss_mib_por_1000_docs_mediana"] = resumen(rss_1000)[0]
        salida.append(c)

    destino = carpeta / "resumen"
    destino.mkdir(exist_ok=True)
    with open(destino / "celdas.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(salida[0]))
        w.writeheader()
        w.writerows(salida)
    escribir_markdown(salida, destino / "celdas.md", docs)
    graficas(salida, carpeta, destino / "graficas")
    print(f"{len(filas)} corridas, {len(salida)} celdas -> {destino}")


def fmt(v, dec=1):
    if v is None:
        return "—"
    return f"{v:,.{dec}f}"


def escribir_markdown(celdas, ruta, docs):
    lineas = [
        "Mediana de las repeticiones; entre paréntesis, mínimo–máximo. Latencias en ms, solo de repeticiones "
        "completas (`ok` o `spill`). "
        f"M3: Q1 lee {docs['Q1']:,} documentos por consulta y Q2 {docs['Q2']:,}.", "",
        "| Motor | Perfil | Consulta | Clientes | Estado | QPS | P95 | P99 | Throttling (s) | RSS máx (MiB) "
        "| Spill | Muestras mín. | CPU ms / 1k docs | RSS MiB / 1k docs |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in celdas:
        p99 = fmt(c["latency_p99_ms_mediana"])
        if c["latency_p99_ms_mediana"] is not None and c["repeticiones"] > 1:
            p99 += f" ({fmt(c['latency_p99_ms_min'])}–{fmt(c['latency_p99_ms_max'])})"
        lineas.append(
            f"| {NOMBRE[c['engine']]} | {CORTO[c['profile']]} | {c['query_id']} | {c['concurrency']} | {c['status']} "
            f"| {fmt(c['throughput_qps_mediana'], 3)} | {fmt(c['latency_p95_ms_mediana'])} | {p99} "
            f"| {fmt(c['cpu_throttled_seconds_mediana'])} | {fmt(c['max_rss_mb_mediana'])} | {c['disk_spill']} "
            f"| {c['muestras_min'] if c['muestras_min'] is not None else '—'} "
            f"| {fmt(c['cpu_ms_por_1000_docs_mediana'], 2)} | {fmt(c['rss_mib_por_1000_docs_mediana'], 3)} |")
    ruta.write_text("\n".join(lineas) + "\n")


# ---------------------------------------------------------------------------------------------------
# Gráficas

def estilo(plt):
    plt.rcParams.update({
        "figure.facecolor": SUPERFICIE, "axes.facecolor": SUPERFICIE, "savefig.facecolor": SUPERFICIE,
        "axes.edgecolor": REJILLA, "axes.labelcolor": TINTA_2, "axes.titlecolor": TINTA,
        "xtick.color": TINTA_2, "ytick.color": TINTA_2, "text.color": TINTA, "axes.grid": True,
        "grid.color": REJILLA, "grid.linewidth": 0.6, "axes.spines.top": False, "axes.spines.right": False,
        "font.size": 9, "axes.titlesize": 10, "legend.frameon": False, "lines.linewidth": 2,
    })


def serie(celdas, motor, perfil, qid, kpi):
    xs, ys, err_bajo, err_alto, sin_dato = [], [], [], [], []
    for conc in CONCURRENCIAS:
        c = next((c for c in celdas if (c["engine"], c["profile"], c["query_id"], c["concurrency"])
                  == (motor, perfil, qid, conc)), None)
        if not c:
            continue
        med = c[f"{kpi}_mediana"]
        if med is None:
            sin_dato.append((conc, c["status"]))
            continue
        xs.append(conc)
        ys.append(med)
        err_bajo.append(med - c[f"{kpi}_min"])
        err_alto.append(c[f"{kpi}_max"] - med)
    return xs, ys, [err_bajo, err_alto], sin_dato


def encabezado(fig, titulo, motores, alto_pulgadas):
    """Título y, en su propia línea debajo, la leyenda; devuelve la fracción superior que ocupan."""
    from matplotlib.lines import Line2D
    reserva = 0.62 / alto_pulgadas
    fig.suptitle(titulo, x=0.01, y=1 - 0.08 / alto_pulgadas, ha="left", va="top", fontsize=11)
    fig.legend([Line2D([], [], color=COLOR[m], marker=MARCADOR[m], markersize=7) for m in motores],
               [NOMBRE[m] for m in motores], loc="upper left", ncol=len(motores),
               bbox_to_anchor=(0.005, 1 - 0.3 / alto_pulgadas))
    return 1 - reserva


def graficas(celdas, carpeta, destino):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    estilo(plt)
    destino.mkdir(parents=True, exist_ok=True)
    consultas = sorted({c["query_id"] for c in celdas})
    motores = [m for m in MOTORES if any(c["engine"] == m for c in celdas)]
    perfiles = [p for p in PERFILES if any(c["profile"] == p for c in celdas)]

    # 1. P99 vs concurrencia: filas = consultas, columnas = perfiles; barras = mínimo–máximo de las repeticiones
    alto = 2.9 * len(consultas) + 0.7
    fig, ejes = plt.subplots(len(consultas), len(perfiles), figsize=(3.4 * len(perfiles), alto),
                             squeeze=False, sharey="row")
    for i, qid in enumerate(consultas):
        for j, perfil in enumerate(perfiles):
            ax = ejes[i][j]
            avisos = []
            for m in motores:
                xs, ys, err, sin_dato = serie(celdas, m, perfil, qid, "latency_p99_ms")
                if xs:
                    ax.errorbar(xs, ys, yerr=err, color=COLOR[m], marker=MARCADOR[m], markersize=6, capsize=3,
                                elinewidth=1)
                avisos += [f"{NOMBRE[m].split()[0]} {conc}: {st}" for conc, st in sin_dato]
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_xticks(CONCURRENCIAS, [str(c) for c in CONCURRENCIAS])
            ax.minorticks_off()
            ax.set_title(f"{qid} · perfil {CORTO[perfil]}", loc="left")
            if avisos:  # celdas sin latencias (p. ej., OOM): se nombran, no se dibujan
                ax.text(0.02, 0.02, "Sin consultas exitosas:\n" + "\n".join(avisos), transform=ax.transAxes,
                        fontsize=7, color=TINTA_2, va="bottom")
            if j == 0:
                ax.set_ylabel("P99 (ms, escala log)")
            if i == len(consultas) - 1:
                ax.set_xlabel("Clientes concurrentes")
    tope = encabezado(fig, "Latencia P99 frente a concurrencia", motores, alto)
    fig.tight_layout(rect=(0, 0, 1, tope))
    fig.savefig(destino / "p99_vs_concurrencia.png", dpi=160)
    plt.close(fig)

    # 2. Throttling vs QPS: una gráfica por consulta; forma del marcador = motor, etiqueta = perfil y clientes
    alto = 4.4
    fig, ejes = plt.subplots(1, len(consultas), figsize=(5.2 * len(consultas), alto), squeeze=False)
    for j, qid in enumerate(consultas):
        ax = ejes[0][j]
        for m in motores:
            pts = [c for c in celdas if c["engine"] == m and c["query_id"] == qid
                   and c["cpu_throttled_seconds_mediana"] is not None and c["throughput_qps_mediana"]]
            ax.scatter([c["cpu_throttled_seconds_mediana"] for c in pts], [c["throughput_qps_mediana"] for c in pts],
                       color=COLOR[m], marker=MARCADOR[m], s=42, edgecolors=SUPERFICIE, linewidths=1.5)
            for c in pts:
                if c["concurrency"] == 50:  # etiquetas selectivas: solo la concurrencia máxima
                    ax.annotate(f"{CORTO[c['profile']]}·50", (c["cpu_throttled_seconds_mediana"],
                                c["throughput_qps_mediana"]), textcoords="offset points", xytext=(5, 3),
                                fontsize=7, color=TINTA_2)
        ax.set_xscale("symlog", linthresh=1)
        ax.set_yscale("log")
        ax.set_title(qid, loc="left")
        ax.set_xlabel("CPU estrangulada en la ventana (s, mediana)")
        if j == 0:
            ax.set_ylabel("Throughput (consultas/s, escala log)")
    tope = encabezado(fig, "Throttling de CPU frente a throughput (cada punto es una celda)", motores, alto)
    fig.tight_layout(rect=(0, 0, 1, tope))
    fig.savefig(destino / "throttling_vs_qps.png", dpi=160)
    plt.close(fig)

    # 3. RSS máximo vs concurrencia, con el límite de memoria del perfil
    alto = 2.9 * len(consultas) + 0.7
    fig, ejes = plt.subplots(len(consultas), len(perfiles), figsize=(3.4 * len(perfiles), alto),
                             squeeze=False, sharey="col")
    for i, qid in enumerate(consultas):
        for j, perfil in enumerate(perfiles):
            ax = ejes[i][j]
            for m in motores:
                xs, ys, err, _ = serie(celdas, m, perfil, qid, "max_rss_mb")
                if xs:
                    ax.errorbar(xs, ys, yerr=err, color=COLOR[m], marker=MARCADOR[m], markersize=6, capsize=3,
                                elinewidth=1)
            ax.axhline(LIMITE_MIB[perfil], color=TINTA_2, linewidth=1, linestyle="--")
            ax.text(1, LIMITE_MIB[perfil], "límite", fontsize=7, color=TINTA_2, va="bottom")
            ax.set_xscale("log")
            ax.set_xticks(CONCURRENCIAS, [str(c) for c in CONCURRENCIAS])
            ax.minorticks_off()
            ax.set_ylim(0, LIMITE_MIB[perfil] * 1.1)
            ax.set_title(f"{qid} · perfil {CORTO[perfil]}", loc="left")
            if j == 0:
                ax.set_ylabel("RSS máximo (MiB)")
            if i == len(consultas) - 1:
                ax.set_xlabel("Clientes concurrentes")
    tope = encabezado(fig, "Memoria residente máxima frente a concurrencia", motores, alto)
    fig.tight_layout(rect=(0, 0, 1, tope))
    fig.savefig(destino / "rss_vs_concurrencia.png", dpi=160)
    plt.close(fig)

    # 4. Curva de RSS en el tiempo: Q1, perfil C, 50 clientes, repetición 1 (la celda más exigente)
    for qid in consultas:
        corridas = {m: carpeta / "runs" / f"{m}_C_{qid}_c50_r1" for m in motores}
        corridas = {m: r for m, r in corridas.items() if (r / "metricas.csv.gz").exists()}
        if not corridas:
            continue
        fig, ax = plt.subplots(figsize=(7.2, 3.4))
        for m, r in corridas.items():
            meta = json.loads((r / "meta.json").read_text())
            t0 = meta["fases"]["carga_inicio"]
            datos = [x for x in csv.DictReader(gzip.open(r / "metricas.csv.gz", "rt")) if x["rss_bytes"]]
            ax.plot([float(x["t"]) - t0 for x in datos], [float(x["rss_bytes"]) / 2 ** 20 for x in datos],
                    color=COLOR[m], label=NOMBRE[m])
            ax.annotate(f"{NOMBRE[m]}: {meta['fila_csv']['status']}", (float(datos[-1]["t"]) - t0,
                        float(datos[-1]["rss_bytes"]) / 2 ** 20), textcoords="offset points", xytext=(-4, 6),
                        ha="right", fontsize=7, color=TINTA_2)
        meta = json.loads((next(iter(corridas.values())) / "meta.json").read_text())
        f = meta["fases"]
        ax.axvspan(f["ventana_inicio"] - f["carga_inicio"], f["ventana_fin"] - f["carga_inicio"], color=REJILLA,
                   alpha=0.5, linewidth=0)
        ax.axhline(LIMITE_MIB["PROFILE_C_SEVERE"], color=TINTA_2, linewidth=1, linestyle="--")
        ax.text(0, LIMITE_MIB["PROFILE_C_SEVERE"], "límite 1,024 MiB", fontsize=7, color=TINTA_2, va="bottom")
        ax.set_ylim(0, 1150)
        ax.set_xlabel("Segundos desde el inicio de la carga (zona sombreada: ventana de medición)")
        ax.set_ylabel("RSS (MiB)")
        ax.set_title(f"Curva de RSS · {qid}, perfil C, 50 clientes, rep. 1", loc="left", pad=22)
        ax.legend(loc="lower right", bbox_to_anchor=(1, 1.0), ncol=len(corridas))
        fig.tight_layout()
        fig.savefig(destino / f"rss_curva_{qid}_C_c50.png", dpi=160)
        plt.close(fig)


if __name__ == "__main__":
    consolidar(RAIZ / (sys.argv[1] if len(sys.argv) > 1 else "results"))
