"""Corre la suite de checks del backend. Un comando en vez de treinta y dos.

POR QUÉ EXISTE
--------------
Cada `check_*.py` es autónomo: arma su propio SQLite temporal, corre y sale con
código 1 si algo falla. Está bien que sea así —se puede correr uno solo mientras
se trabaja en lo que cubre— pero significaba que la suite entera eran treinta y
dos comandos y la memoria de quien revisa. En una serie de seis PRs que agregó
seis checks, nadie los corrió todos juntos hasta que se hizo una auditoría.

LA LISTA ES EXPLÍCITA, Y NO UN GLOB
-----------------------------------
Esto es lo más importante del archivo. `backend/scripts/` tiene tres clases de
script con el mismo prefijo:

  · Los TESTS: arman un SQLite temporal, no tocan nada de nadie. Son estos.
  · Los DIAGNÓSTICOS: se conectan a la `DATABASE_URL` REAL para contar algo.
    `diag/handle_collisions.py` documenta que hay que correrlo contra
    producción.
  · Las MUTACIONES: `reconcile_handles.py` renombra gente.

Un `for f in check_*.py` los mete a todos en la misma bolsa, y contra un entorno
con `DATABASE_URL` apuntando a producción eso es un script de diagnóstico
corriendo en CI. Por eso los diagnósticos se mudaron a `scripts/diag/` y por eso
esta lista se escribe a mano: agregar un check es una línea acá, y es una línea
que obliga a decidir de qué clase es el script nuevo.

Uso:
    python backend/scripts/run_checks.py            # todos
    python backend/scripts/run_checks.py xp handles # solo los que matcheen
    python backend/scripts/run_checks.py --list

Sale con código 1 si alguno falla.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# Los checks autocontenidos, en orden alfabético para que agregar uno sea obvio.
# NO incluye nada que lea la base real (ver `diag/`) ni nada que escriba.
CHECKS = [
    "check_adaptive_session_size",
    "check_aforo",
    "check_alias_vocabulario",
    "check_avisos_de_evento",
    "check_cafecito_email",
    "check_cafecito_stream",
    "check_concurrencia",
    "check_dashboard",
    "check_exercise_cycle_no_repeat",
    "check_game_api",
    "check_game_chat",
    "check_game_dashboard",
    "check_game_events",
    "check_game_explain",
    "check_game_generator",
    "check_game_ranking_sort",
    "check_game_referrals",
    "check_game_simulation",
    "check_game_stats",
    "check_game_unlocks",
    "check_game_username",
    "check_handles",
    "check_mails_cafecito_reclutas",
    "check_opciones_equivalentes",
    "check_openapi_sync",
    "check_pedidos_del_resumen",
    "check_race_get_or_create",
    "check_ranking_clasico",
    "check_reclutas_cruzados",
    "check_report_thanks_email",
    "check_resumen_de_sesion",
    "check_schema_migrations",
    "check_survey_channel_d",
    "check_table_boost",
    "check_xp_boost",
]

# Cuántos a la vez. Cada uno abre su propio SQLite en un directorio temporal, así
# que no se pisan; el techo está en el CPU y en que la salida siga siendo legible.
PARALELOS = 4

# Ninguno tarda más de un minuto hoy. El tope está para que un check que se
# cuelgue —una espera sin timeout, un `input()` olvidado— no cuelgue la corrida
# entera sin decir cuál fue.
TIMEOUT_S = 300


def correr(nombre: str) -> tuple[str, bool, float, str]:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / f"{nombre}.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_S,
        )
        ok = proc.returncode == 0
        salida = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        ok, salida = False, f"se colgó: más de {TIMEOUT_S}s sin terminar"
    return nombre, ok, time.monotonic() - t0, salida


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "filtros",
        nargs="*",
        help="corre solo los checks cuyo nombre contenga alguno de estos textos",
    )
    ap.add_argument("--list", action="store_true", help="listar y salir")
    args = ap.parse_args()

    elegidos = [
        c for c in CHECKS
        if not args.filtros or any(f in c for f in args.filtros)
    ]
    if args.list:
        for c in elegidos:
            print(c)
        return 0
    if not elegidos:
        print(f"ningún check matchea {args.filtros}")
        return 1

    faltantes = [c for c in elegidos if not (SCRIPTS / f"{c}.py").exists()]
    if faltantes:
        # La lista es a mano, así que puede quedar apuntando a un archivo que se
        # renombró. Es un error de la lista, no de los checks: se dice y se corta.
        print("la lista de CHECKS nombra archivos que no existen:")
        for c in faltantes:
            print(f"  - {c}.py")
        return 1

    t0 = time.monotonic()
    fallaron: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=PARALELOS) as pool:
        for nombre, ok, segundos, salida in pool.map(correr, elegidos):
            print(f"{'ok   ' if ok else 'FALLA'}  {nombre:<34} {segundos:5.1f}s")
            if not ok:
                fallaron.append((nombre, salida))

    print(f"\n{len(elegidos)} checks en {time.monotonic() - t0:.0f}s")
    if not fallaron:
        print("todo ok")
        return 0

    # La salida de los que fallaron va AL FINAL y entera. Intercalada con la de
    # los demás —y con cuatro corriendo a la vez— no se puede leer.
    for nombre, salida in fallaron:
        print(f"\n{'─' * 70}\n{nombre}\n{'─' * 70}")
        print(salida.rstrip())
    print(f"\n{len(fallaron)} FALLARON: " + ", ".join(n for n, _ in fallaron))
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
