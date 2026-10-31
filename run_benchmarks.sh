#!/usr/bin/env bash
# Automatiza la matriz de mediciones (motor × perfil × consulta × concurrencia × repetición).
#
# Uso:
#   ./run_benchmarks.sh check     Verifica que el host aplica los límites de cgroups v2 y deja
#                                 constancia de sus especificaciones (sección 1 del reporte).
#   ./run_benchmarks.sh piloto    Celda piloto (postgres, perfil C, Q1, 50 clientes) en results/piloto/.
#   ./run_benchmarks.sh matriz    Matriz completa por rondas (repetición 1 de todas las celdas, luego 2
#                                 y 3). Retoma donde se quedó: salta las corridas ya registradas en
#                                 results/mediciones.csv.
#   ./run_benchmarks.sh variantes Experimento complementario: Q1 en PostgreSQL con work_mem de 64MB
#                                 (27 corridas, por rondas) en results/variantes/.
#   ./run_benchmarks.sh corrida <engine> <A|B|C> <Q1|Q2> <concurrencia> <repetición> [opciones del runner]
set -euo pipefail

cd "$(dirname "$0")"
PY=".venv/bin/python"
MOTORES=(mongo postgres)
PERFILES=(A B C)
CONSULTAS=(Q1 Q2)
CONCURRENCIAS=(1 10 50)

IMG="alpine:3.20"

cmd_check() {
  local fail=0
  ok()  { printf '  [OK]    %s\n' "$*"; }
  bad() { printf '  [FALLA] %s\n' "$*"; fail=1; }

  echo "== Host"
  echo "  fecha: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "  sistema: $(uname -srm)"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "  cpu: $(sysctl -n machdep.cpu.brand_string) ($(sysctl -n hw.ncpu) núcleos)"
    echo "  memoria: $(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 )) GiB"
  else
    echo "  cpu: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | xargs) ($(nproc) núcleos)"
    echo "  memoria: $(awk '/MemTotal/ {printf "%d GiB", $2/1024/1024}' /proc/meminfo)"
  fi
  echo "  python: $(python3 --version 2>&1 || echo 'no instalado')"

  echo "== Docker"
  docker info --format '  servidor: {{.ServerVersion}} | {{.OperatingSystem}} | kernel {{.KernelVersion}} | {{.Architecture}}
  recursos del daemon: {{.NCPU}} CPU, {{.MemTotal}} bytes
  cgroups: v{{.CgroupVersion}} ({{.CgroupDriver}}) | almacenamiento: {{.Driver}}'

  [[ "$(docker info --format '{{.CgroupVersion}}')" == "2" ]] && ok "cgroups v2" || bad "se requiere cgroups v2"
  local mem_bytes
  mem_bytes=$(docker info --format '{{.MemTotal}}')
  # Perfil A (4 GiB) + telemetría + margen
  (( mem_bytes >= 6 * 1024**3 )) && ok "memoria del daemon >= 6 GiB" || bad "memoria del daemon < 6 GiB: el perfil A no cabe con la telemetría"
  (( $(docker info --format '{{.NCPU}}') >= 6 )) && ok "CPU del daemon >= 6" || bad "CPU del daemon < 6"

  echo "== Límites del perfil C aplicados por cgroups (1 CPU, 1 GiB, sin swap)"
  local limits cpu_max mem_max swap_max
  limits=$(docker run --rm --cpus=1 --memory=1024m --memory-swap=1024m "$IMG" sh -c \
    'echo "$(cat /sys/fs/cgroup/cpu.max)|$(cat /sys/fs/cgroup/memory.max)|$(cat /sys/fs/cgroup/memory.swap.max)"')
  IFS='|' read -r cpu_max mem_max swap_max <<< "$limits"
  echo "  cpu.max=$cpu_max memory.max=$mem_max memory.swap.max=$swap_max"
  [[ "$cpu_max" == "100000 100000" ]] && ok "cuota de 1 CPU" || bad "cpu.max inesperado"
  [[ "$mem_max" == "1073741824" ]]    && ok "límite de 1 GiB" || bad "memory.max inesperado"
  [[ "$swap_max" == "0" ]]            && ok "swap desactivado (memory-swap = memory)" || bad "el contenedor puede usar swap"

  echo "== Throttling CFS (2 hilos ocupados con 1 CPU durante 3 s)"
  local stat throttled
  stat=$(docker run --rm --cpus=1 "$IMG" sh -c \
    'for i in 1 2; do (while :; do :; done) & done; sleep 3; cat /sys/fs/cgroup/cpu.stat; kill %1 %2')
  echo "$stat" | grep -E 'nr_periods|nr_throttled|throttled_usec' | sed 's/^/  /'
  throttled=$(echo "$stat" | awk '/^nr_throttled/ {print $2}')
  (( throttled > 0 )) && ok "el throttling se registra en cpu.stat" || bad "no se registró throttling"

  echo "== Limpieza de caché del sistema (drop_caches)"
  docker run --rm --privileged "$IMG" sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches' \
    && ok "drop_caches disponible desde un contenedor privilegiado" || bad "no se pudo limpiar la caché"

  echo
  if (( fail )); then echo "RESULTADO: el entorno NO cumple"; return 1; fi
  echo "RESULTADO: el entorno cumple"
}

# En macOS impide que el equipo se suspenda mientras dure el script
mantener_despierto() {
  if command -v caffeinate >/dev/null; then caffeinate -dims -w $$ & fi
}

perfil_id() { case "$1" in A) echo PROFILE_A_BASELINE ;; B) echo PROFILE_B_MEDIUM ;; C) echo PROFILE_C_SEVERE ;; esac; }

registrada() {  # csv engine perfil query_id concurrencia repetición
  [[ -f "$1" ]] && awk -F, -v e="$2" -v p="$(perfil_id "$3")" -v q="$4" -v c="$5" -v r="$6" \
    '$2==e && $3==p && $4==q && $5==c && $6==r {f=1} END {exit !f}' "$1"
}

cmd_corrida() {
  "$PY" benchmark_runner/runner.py --engine "$1" --profile "$2" --query "$3" --concurrency "$4" --run "$5" "${@:6}"
}

cmd_piloto() {
  mantener_despierto
  cmd_corrida postgres C Q1 50 1 --salida results/piloto
}

cmd_matriz() {
  mantener_despierto
  local total=0 hechas=0
  for r in 1 2 3; do
    for e in "${MOTORES[@]}"; do for p in "${PERFILES[@]}"; do for q in "${CONSULTAS[@]}"; do
      for c in "${CONCURRENCIAS[@]}"; do
        total=$((total + 1))
        if registrada results/mediciones.csv "$e" "$p" "$q" "$c" "$r"; then hechas=$((hechas + 1)); continue; fi
        echo "== $(date '+%H:%M:%S') corrida $((hechas + 1)) de 108"
        # Una falla del script no detiene la matriz: queda en el log y la corrida se repite al relanzar
        cmd_corrida "$e" "$p" "$q" "$c" "$r" || echo "== FALLA del runner en ${e}_${p}_${q}_c${c}_r${r}; se repetirá al relanzar"
        hechas=$((hechas + 1))
      done
    done; done; done
  done
  echo "Matriz completa: $hechas de $total corridas en results/mediciones.csv"
}

cmd_variantes() {
  mantener_despierto
  local hechas=0
  for r in 1 2 3; do for p in "${PERFILES[@]}"; do for c in "${CONCURRENCIAS[@]}"; do
    if ! registrada results/variantes/mediciones.csv postgres "$p" Q1_WM64 "$c" "$r"; then
      echo "== $(date '+%H:%M:%S') variante $((hechas + 1)) de 27"
      cmd_corrida postgres "$p" Q1 "$c" "$r" --work-mem 64MB --salida results/variantes \
        || echo "== FALLA del runner en postgres_${p}_Q1_WM64_c${c}_r${r}; se repetirá al relanzar"
    fi
    hechas=$((hechas + 1))
  done; done; done
  echo "Variantes completas: $hechas de 27 corridas en results/variantes/mediciones.csv"
}

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
  # Cada verificación se agrega con su fecha: queda constancia del entorno de cada sesión de medición
  check)   { echo "######## $(date '+%Y-%m-%d %H:%M:%S %Z')"; cmd_check; echo; } | tee -a results/entorno.txt ;;
  piloto)  cmd_piloto ;;
  matriz)  cmd_matriz ;;
  variantes) cmd_variantes ;;
  corrida) shift; cmd_corrida "$@" ;;
  *)       usage; exit 1 ;;
esac
