"""Cambia los @ autogenerados viejos (`modulo4124`) por los nuevos (`casifinal`).

Arranca EN SECO: sin `--aplicar` imprime el mapeo y no escribe nada.

**Qué renombra, y qué no.** El filtro NO es `alias_is_generated`. Esa columna
está en `true` también para los jugadores registrados cuyo alias salió de su
username de Intervalo (`aliases.alias_for_user`), así que filtrar por ahí le
cambiaría el nombre a `@mcragnolini`, `@fenolftaleina` o `@goldenmedialuna`, que
están mostrando el suyo de verdad. El filtro es el PATRÓN del generador viejo
—una de sus 30 palabras seguida de 3 o 4 dígitos, o `jugador<n>`— Y además
`alias_is_generated = true`, porque hay al menos una persona (`@primo5957`) que
eligió a mano un @ con esa forma y no hay que tocarla.

**Por qué pasa por `handles.reclamar` y no por un UPDATE.** El @ viejo no se
libera: queda `retired` y sigue resolviendo los links `?r=` que esa persona
repartió (ver backend/handles.py). Un `UPDATE game_players SET alias` dejaría el
registro mintiendo y el @ viejo suelto para que lo tome otro — que es exactamente
el fallo que el módulo existe para impedir.

**Determinismo.** La semilla es fija, así que la corrida en seco y la de verdad
producen el MISMO mapeo. Es lo que permite revisar la lista antes de aplicarla.

Uso:
    python backend/scripts/diag/rebautizar_alias.py             # en seco
    python backend/scripts/diag/rebautizar_alias.py --aplicar   # escribe
"""

from __future__ import annotations

import os
import random
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent))

if not os.environ.get("DATABASE_URL"):
    print("Falta DATABASE_URL: este script se corre contra la base REAL.")
    sys.exit(2)

import database  # noqa: E402
import handles  # noqa: E402
from models import GamePlayer  # noqa: E402
from game.aliases import COMBINACIONES, _MAX_LEN, retire_alias  # noqa: E402
from usernames import validate_username  # noqa: E402

# Las 30 palabras del generador viejo. Se copian acá y no se importan de
# `aliases` porque el objetivo es justamente que dejen de estar allá: el día que
# el vocabulario viejo se borre, este script tiene que seguir sabiendo a quién
# renombró.
_VIEJAS = (
    "derivador", "tangente", "pendiente", "limite", "integral", "funcion",
    "parabola", "vertice", "maximo", "minimo", "euler", "newton", "leibniz",
    "matriz", "vector", "escalar", "factorial", "primo", "modulo", "seno",
    "coseno", "exponente", "cociente", "producto", "curva", "recta",
    "asintota", "dominio", "imagen", "abscisa",
)
PATRON_VIEJO = re.compile(r"^(?:(?:" + "|".join(_VIEJAS) + r")\d{3,4}|jugador\d+)$")

# Fija a propósito: la corrida en seco tiene que dar el mismo mapeo que la real,
# o revisar la lista antes de aplicarla no sirve de nada.
SEMILLA = 20260908

APLICAR = "--aplicar" in sys.argv


def _tomados(db) -> set[str]:
    """Todo lo que ya es de alguien, en los tres lugares donde puede estar."""
    from models import Handle, User

    out = {h for (h,) in db.query(Handle.handle)}
    out |= {a for (a,) in db.query(GamePlayer.alias) if a}
    out |= {u for (u,) in db.query(User.username) if u}
    return out


def main() -> int:
    db = database.SessionLocal()
    jugadores = (
        db.query(GamePlayer)
        .filter(GamePlayer.is_bot.is_(False))
        .order_by(GamePlayer.exercises_correct.desc(), GamePlayer.id)
        .all()
    )
    objetivo = [
        p for p in jugadores
        if p.alias and PATRON_VIEJO.match(p.alias) and p.alias_is_generated
    ]
    conservan = [
        p for p in jugadores
        if p.alias and PATRON_VIEJO.match(p.alias) and not p.alias_is_generated
    ]

    tomados = _tomados(db)
    rng = random.Random(SEMILLA)
    mapa: list[tuple[GamePlayer, str]] = []
    for p in objetivo:
        nuevo = None
        for _ in range(400):
            cand = rng.choice(COMBINACIONES)
            if cand not in tomados:
                nuevo = cand
                break
        if nuevo is None:
            for _ in range(400):
                base = rng.choice(COMBINACIONES)
                cand = f"{base[: _MAX_LEN - 2]}{rng.randint(2, 99)}"
                if cand not in tomados:
                    nuevo = cand
                    break
        if nuevo is None:
            print(f"  !! sin nombre libre para @{p.alias}, se lo saltea")
            continue
        ok, motivo = validate_username(nuevo)
        if not ok:
            print(f"  !! candidato inválido {nuevo!r}: {motivo}")
            continue
        tomados.add(nuevo)
        mapa.append((p, nuevo))

    print(f"jugadores reales: {len(jugadores)}")
    print(f"con el patrón viejo: {len(objetivo) + len(conservan)}"
          f"  ->  se renombran {len(mapa)}, se respetan {len(conservan)} "
          f"(lo eligieron a mano: {', '.join('@' + p.alias for p in conservan) or 'ninguno'})")
    print()
    for p, nuevo in mapa:
        uni = p.university or "-"
        print(f"  @{p.alias:<16s} -> @{nuevo:<17s} {uni:6s} {p.exercises_correct:3d} correctas")

    if not APLICAR:
        print()
        print(f"EN SECO: no se escribió nada. Volvé a correr con --aplicar para aplicar los {len(mapa)}.")
        return 0

    print()
    hechos = 0
    for p, nuevo in mapa:
        viejo = p.alias
        try:
            # El registro es la autoridad: retira el @ anterior del mismo dueño
            # y deja el nuevo activo. `retire_alias` mantiene además la tabla
            # vieja de historia en sincronía, que es la red de contención.
            handles.reclamar(db, nuevo, player_id=p.id)
            retire_alias(db, viejo, p.id)
            p.alias = nuevo
            p.alias_is_generated = True
            db.commit()
            hechos += 1
        except Exception as e:  # noqa: BLE001
            db.rollback()
            print(f"  !! falló @{viejo} -> @{nuevo}: {type(e).__name__}: {e}")
    print(f"listo: {hechos}/{len(mapa)} renombrados. Los @ viejos quedan retirados, "
          f"así que los links ?r= que repartieron siguen funcionando.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
