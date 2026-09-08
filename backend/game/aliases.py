"""Aliases autogenerados para guests del minijuego.

Formato palabra+número (ej. "derivador7431"), compatible con las reglas de
usernames.validate_username. Namespace propio: la unicidad es contra
game_players.alias, no contra users.username.
"""

from __future__ import annotations

import random
from datetime import datetime

from sqlalchemy.orm import Session

import handles
from models import GameAliasHistory, GamePlayer

# El vocabulario del @ que le toca a quien todavía no eligió el suyo.
#
# Antes era palabra-de-matemática + cuatro dígitos: `modulo4124`, `coseno2342`,
# `asintota8447`. Ese formato tiene dos problemas y los dos se ven en el
# ranking. El número delata que el nombre no lo eligió nadie, así que la tabla
# se lee como una lista de anónimos; y la palabra viene del temario, o sea que
# el juego te bautiza con la materia de la que te querés olvidar.
#
# Lo que reemplaza a eso sale de mirar los @ que la gente SÍ eligió cuando pudo:
# `goldenmedialuna`, `vitteltone`, `chuchubanana`, `gatitosensual`. Ninguno
# tiene números y todos son la misma broma — algo de acá, dicho en joda. Así
# que el generador arma `modificador + sustantivo` con comida rioplatense y
# vida de cursada, sin dígitos: `casifinal`, `triplechoripan`, `puromate`.
#
# Reglas que el check verifica y que hay que respetar al agregar palabras:
#   - solo [a-z0-9._] (usernames._VALID_RE): nada de ñ ni acentos;
#   - el combinado entero entra en _MAX_LEN, si no la combinación se descarta;
#   - nada que pueda leerse como un insulto: el @ se lo asignamos nosotros a
#     alguien que no lo eligió, y aparece al lado de su nombre en el ranking.
_MODIFICADORES = (
    "golden", "doble", "triple", "medio", "casi", "puro", "full", "super",
    "mega", "mini", "master", "turbo", "ultra", "sobre",
)

_SUSTANTIVOS = (
    # La mesa
    "medialuna", "alfajor", "milanesa", "choripan", "empanada", "fernet",
    "mate", "factura", "asado", "locro", "provoleta", "pionono", "chipa",
    "tostado", "submarino", "matambre", "flan", "bondiola", "vitteltone",
    "ravioles", "noquis", "pastafrola", "budin", "criollito", "torta",
    "helado", "granizado", "sanguche", "pizzeta", "canelones",
    # La cursada
    "apunte", "parcial", "final", "cursada", "fotocopia", "termo", "birome",
    "carpeta", "resumen", "pizarron", "aula", "cantina", "bondi", "subte",
    "mochila", "teorica", "practica", "coloquio", "libreta", "recreo",
)

_MAX_LEN = 15
_ATTEMPTS = 40


def _combinaciones() -> tuple[str, ...]:
    """Todos los @ que el generador puede producir, en orden estable.

    Se calcula una vez y se filtra por largo acá y no al sortear: si una
    combinación no entra en `_MAX_LEN` no existe, en vez de salir recortada a
    la mitad de una palabra (`mastervitteltone` → `mastervitteltO`). El
    sustantivo solo también cuenta: `vitteltone` y `medialuna` se sostienen sin
    prefijo, y son justo los que mejor suenan.
    """
    fuera = [n for n in _SUSTANTIVOS if len(n) <= _MAX_LEN]
    combinadas = [
        m + n
        for m in _MODIFICADORES
        for n in _SUSTANTIVOS
        if len(m) + len(n) <= _MAX_LEN
    ]
    return tuple(fuera + combinadas)


COMBINACIONES = _combinaciones()


def alias_taken(db: Session, alias: str) -> bool:
    """¿Este @ está en uso, o lo estuvo alguna vez?

    Delega en el registro (backend/handles.py), que es la única autoridad. Los
    soltados siguen contando como tomados —un @ viejo sigue resolviendo links de
    reclutamiento, así que dárselo a otra persona sería darle también la gente
    que trajo la primera— y ahora además cuentan los `users.username`, que este
    módulo no miraba y por eso podía entregar un nombre que ya era de alguien.
    """
    return handles.tomado(db, alias)


def retire_alias(db: Session, alias: str, player_id: int) -> None:
    """Deja anotado que ese @ fue de este jugador. No commitea.

    Se llama al cambiar de @ y al fusionar un invitado con una cuenta —los dos
    momentos en que un @ deja de existir con alguien todavía compartiéndolo por
    ahí.
    """
    if not alias:
        return
    # `handles.reclamar` ya retira el @ anterior del mismo dueño, así que este
    # camino queda solo para el resto del código que todavía llama a
    # `retire_alias` explícitamente. Se mantiene `game_alias_history` en sincronía
    # un release más como red de contención: si hay que volver atrás, lo que se
    # muere si no son los links `?r=` repartidos.
    fila = handles.duenio(db, alias)
    if fila is not None and fila.status == "active":
        fila.status = "retired"
        fila.released_at = datetime.utcnow()
        fila.player_id = player_id
    ya = db.query(GameAliasHistory).filter(GameAliasHistory.alias == alias).first()
    if ya is not None:
        # El @ vuelve a soltarse (A→B→A→C): gana el dueño más reciente, que es
        # a quien apuntan los links que se están repartiendo hoy.
        ya.player_id = player_id
        ya.released_at = datetime.utcnow()
        return
    db.add(GameAliasHistory(alias=alias, player_id=player_id, released_at=datetime.utcnow()))


def generate_guest_alias(db: Session, rng: random.Random | None = None) -> str:
    """Elige palabra+sufijo libre. Igual que assign_unique_username, el chequeo
    es un SELECT y el INSERT viene después: llamar desde un loop que capture
    IntegrityError y reintente."""
    rng = rng or random.Random()
    # El camino normal: tirar al azar y preguntar. Con el pozo mayormente libre
    # sale en uno o dos intentos, y cada intento es un SELECT por índice.
    for _ in range(_ATTEMPTS):
        candidate = rng.choice(COMBINACIONES)
        if not alias_taken(db, candidate):
            return candidate

    # Cuarenta tiros fallados significa que el pozo está casi lleno, no
    # necesariamente agotado: con 741 de 744 tomados, la chance de pegarle a
    # uno de los tres libres tirando al azar es del 15%. Antes de resignarse a
    # los dígitos, se pregunta de una cuáles quedan. Es UNA consulta y solo la
    # paga el caso raro; sin esto los últimos nombres del pozo no se entregaban
    # nunca y el `casifinal2` aparecía mucho antes de hacer falta.
    libres = [c for c in COMBINACIONES if not alias_taken(db, c)]
    if libres:
        return rng.choice(libres)

    # Ahora sí, agotado de verdad. Un `casifinal37` se sigue leyendo mejor que
    # un `modulo4124`. Los @ no se liberan nunca —un @ viejo sigue resolviendo
    # links `?r=`— así que el pozo solo se achica y este camino es el final.
    for _ in range(_ATTEMPTS):
        base = rng.choice(COMBINACIONES)
        suffix = str(rng.randint(2, 99))
        candidate = f"{base[: _MAX_LEN - len(suffix)]}{suffix}"
        if not alias_taken(db, candidate):
            return candidate
    # Salida determinística si el azar viene repetido: sufijo incremental.
    n = 100
    while True:
        candidate = f"jugador{n}"
        if not alias_taken(db, candidate):
            return candidate
        n += 1


def alias_for_user(db: Session, username: str | None, name: str | None) -> str:
    """Alias inicial de un jugador registrado: su username de Intervalo si está
    libre en el namespace del juego; si no, variantes con sufijo."""
    base = (username or "").strip() or None
    if base is None:
        return generate_guest_alias(db)
    if not alias_taken(db, base):
        return base
    n = 2
    while True:
        suffix = str(n)
        candidate = f"{base[: _MAX_LEN - len(suffix)]}{suffix}"
        if not alias_taken(db, candidate):
            return candidate
        n += 1
