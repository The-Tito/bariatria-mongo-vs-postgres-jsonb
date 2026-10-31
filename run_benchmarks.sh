#!/usr/bin/env bash
# Automatiza la matriz de mediciones (motor × perfil × consulta × concurrencia × repetición).
#
# Uso:
#   ./run_benchmarks.sh check    Verifica que el host aplica los límites de cgroups v2 y deja
#                                constancia de sus especificaciones (sección 1 del reporte).
set -euo pipefail

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

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
  check) cmd_check ;;
  *)     usage; exit 1 ;;
esac
