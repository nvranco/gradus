"""Verifica el vocabulario de @ autogenerados (game/aliases.py).

El generador le pone nombre a alguien que no lo eligió, y ese nombre aparece al
lado de su puesto en el ranking. Así que las reglas del vocabulario no son
estética: una palabra con ñ produce un @ que `validate_username` rechaza, y una
combinación larga salía recortada a la mitad de una palabra.

Cubre:
  - toda combinación pasa `validate_username` (minúsculas, sin acentos, 3-15);
  - ninguna se recorta: el largo se filtra al armar el pozo, no al sortear;
  - el pozo es lo bastante grande para el padrón de hoy con margen;
  - no hay repetidas ni palabras duplicadas entre las dos listas;
  - ninguna combinación lleva dígitos (era lo que delataba al @ viejo);
  - `generate_guest_alias` no devuelve nunca un @ ya tomado, ni siquiera
    cuando el pozo está casi lleno.

Uso:
    python backend/scripts/check_alias_vocabulario.py
"""

import os
import random
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = "sqlite:///" + str(
    Path(tempfile.mkdtemp()) / "alias.db"
).replace("\\", "/")
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent))

import database  # noqa: E402
import handles  # noqa: E402
from models import Base, GamePlayer  # noqa: E402
from game import aliases  # noqa: E402
from usernames import USERNAME_MAX, validate_username  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  [{'ok' if cond else 'FALLA'}] {label}")
    if not cond:
        FAILURES.append(label)


print("1. cada combinación es un @ válido")
malas = []
for c in aliases.COMBINACIONES:
    ok, motivo = validate_username(c)
    if not ok:
        malas.append((c, motivo))
check(not malas, f"las {len(aliases.COMBINACIONES)} pasan validate_username ({malas[:3]})")
check(all(len(c) <= USERNAME_MAX for c in aliases.COMBINACIONES),
      f"ninguna pasa de {USERNAME_MAX} caracteres")
check(all(not any(ch.isdigit() for ch in c) for c in aliases.COMBINACIONES),
      "ninguna lleva dígitos — eso era lo que delataba al @ viejo")

print("\n2. nada se recorta a la mitad de una palabra")
# Toda combinación tiene que ser o un sustantivo entero, o un modificador
# entero seguido de un sustantivo entero. Si alguna quedara truncada, este
# chequeo la encuentra.
enteras = set(aliases._SUSTANTIVOS) | {
    m + n for m in aliases._MODIFICADORES for n in aliases._SUSTANTIVOS
}
sueltas = [c for c in aliases.COMBINACIONES if c not in enteras]
check(not sueltas, f"todas son palabras completas ({sueltas[:3]})")

print("\n3. las listas están limpias")
check(len(set(aliases._MODIFICADORES)) == len(aliases._MODIFICADORES), "sin modificadores repetidos")
check(len(set(aliases._SUSTANTIVOS)) == len(aliases._SUSTANTIVOS), "sin sustantivos repetidos")
check(not (set(aliases._MODIFICADORES) & set(aliases._SUSTANTIVOS)),
      "ninguna palabra está en las dos listas")
check(len(set(aliases.COMBINACIONES)) == len(aliases.COMBINACIONES), "sin combinaciones repetidas")

print("\n4. el pozo alcanza")
# Los @ no se liberan NUNCA —un @ retirado sigue resolviendo links `?r=`— así
# que el pozo solo se achica. 500 es holgado contra el padrón de hoy (242
# jugadores, 148 a renombrar) y deja margen para varios meses; por debajo de
# eso el generador empieza a caer en el camino con dígitos.
check(len(aliases.COMBINACIONES) >= 500,
      f"hay {len(aliases.COMBINACIONES)} nombres posibles (mínimo 500)")

print("\n5. no entrega un @ tomado, ni con el pozo casi lleno")
Base.metadata.create_all(bind=database.engine)
db = database.SessionLocal()

# Se ocupa TODO el pozo menos tres, para forzar los dos caminos del generador.
#
# Se ocupan por el REGISTRO (`handles`) y no solo con filas de `game_players`,
# porque `alias_taken` pregunta ahí y en ningún otro lado. Escribir solo el
# jugador dejaba el registro vacío, el generador creía que todo estaba libre y
# el que reventaba era el UNIQUE de la tabla — que es exactamente el fallo que
# este check tiene que atrapar del lado del generador y no del de la base.
libres = set(aliases.COMBINACIONES)
reservados = {libres.pop(), libres.pop(), libres.pop()}
for i, nombre in enumerate(sorted(libres)):
    p = GamePlayer(guest_token=f"t{i}", alias=nombre)
    db.add(p)
    db.flush()
    handles.reclamar(db, nombre, player_id=p.id)
db.commit()

rng = random.Random(1)
salidas = []
for _ in range(3):
    a = aliases.generate_guest_alias(db, rng)
    salidas.append(a)
    p = GamePlayer(guest_token=f"n{a}", alias=a)
    db.add(p)
    db.flush()
    handles.reclamar(db, a, player_id=p.id)
    db.commit()
check(len(set(salidas)) == 3, f"tres llamadas seguidas dan tres @ distintos ({salidas})")
check(set(salidas) <= reservados,
      f"y son exactamente los que quedaban libres ({salidas} vs {sorted(reservados)})")

# Ahora el pozo está agotado: tiene que caer al camino con dígitos y seguir
# devolviendo algo válido y libre.
extra = aliases.generate_guest_alias(db, rng)
ok, motivo = validate_username(extra)
check(ok, f"con el pozo agotado sigue devolviendo un @ válido: {extra!r} ({motivo})")
check(not handles.tomado(db, extra), f"y libre en el registro ({extra!r})")

print("\n6. el vocabulario viejo ya no está")
check(not hasattr(aliases, "_WORDS"),
      "no quedó `_WORDS`: las palabras del temario se fueron con el formato viejo")

print()
if FAILURES:
    print(f"{len(FAILURES)} chequeos fallaron:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("todos los chequeos pasaron")
