"""Copy de los avisos del minijuego.

Gemelo de `notification_copy.py` de Intervalo y con la misma mecánica —variantes
con una condición y un render, categoría por sorteo pesado, sin repetir la del
día anterior—, pero con un pool propio. No es duplicación por comodidad: las
siete categorías de Intervalo se apoyan en `unit_states`, `answers`, `sessions` y
`users.total_xp`, y un jugador del minijuego no tiene filas en ninguna de esas.
La XP del juego, además, NUNCA suma a `users.total_xp` (ver models.py).

De aquellas siete se traen dos —`social` y `universidad`, porque el juego tiene
universidad y XP propias— y se descartan tres: `practice` habla de repasos SM-2,
y `podium` y `personal_best` miran tablas de Intervalo.

**Qué se mide con XP y qué con Elo, porque acá es fácil mentir.** El ranking de
PERSONAS del juego va por XP (`ranking.ORDEN_XP`), y la XP es también lo que
multiplica el cafecito: por eso `social_semana` y `social_aporte` hablan de XP y
el número es real.

El ranking de UNIVERSIDADES, en cambio, va por Elo promedio, y es una decisión
deliberada del producto —«la pregunta interesante es cuál deriva mejor, no cuál
tuvo más tiempo libre»— que además es lo que impide que un cafecito compre un
puesto, porque el empuje mueve XP y no mueve θ (game/router.py ::
game_university_leaderboard). Así que ningún aviso puede decir «tu universidad
está a N XP de la otra»: sería un número que la propia tabla del juego
desmiente. `uni_cerca` usa la misma frase sin número que ya usa el feed del
juego, «está a nada de pasar a».

Ojo que en Intervalo clásico es al revés —allá las universidades sí se comparan
por XP semanal (`push_store.university_weekly_xp`)—, y esa diferencia entre los
dos productos es justo la trampa.

El título es `dx` y no `Intervalo`: son dos apps instaladas, con dos íconos
distintos en la pantalla de inicio, y en la bandeja de notificaciones tienen que
poder distinguirse.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

TITULO = "dx"

# A dónde lleva el tap. El service worker lo usa tal cual (ver web/public/sw.js);
# sin esto abría la home de Intervalo, que es otra app.
URL = "/derivadas"

# ── Categorías ───────────────────────────────────────────────────────────────
# Programadas: salen solas, en el horario que la persona eligió, y compiten por
# el único cupo programado del día.
CAT_SOCIAL = "social"
CAT_REACTIVACION = "reactivacion"
CAT_RECORD = "record"

# Reactivas: salen porque PASÓ algo. Tienen su propio cupo (dos por día) y se
# resuelven por orden de prioridad, sin sorteo: la primera que tenga hecho gana.
CAT_EMPUJE = "empuje"
CAT_RECLUTA = "recluta"
CAT_RANKING = "ranking"
CAT_UNIVERSIDAD = "universidad"

PESOS: dict[str, float] = {
    CAT_SOCIAL: 0.40,
    CAT_REACTIVACION: 0.40,
    CAT_RECORD: 0.20,
}

# Los días de silencio en los que sale el aviso de reactivación, y ninguno más.
#
# En Intervalo, `reactivation` dispara cada vez que hay un día sin practicar y
# solo lo frena la rotación de categorías: a alguien que se fue le puede seguir
# llegando «hace N días que no practicás» para siempre. Acá no va, porque el
# juego es de una sentada y la mayoría no vuelve —de 92 personas de la cohorte
# del 24/08, volvieron otro día 5—. Cuatro avisos en dos semanas y se termina.
DIAS_DE_REACTIVACION = (1, 3, 7, 14)

# Después de este silencio se apaga el canal para esa persona. Es hacer con la
# mano lo que ya decidió: un mes sin abrir el juego con los avisos prendidos no
# es alguien a quien le falte un recordatorio.
DIAS_PARA_APAGAR = 30

# Cuántas horas de empuje quedando cuentan como "se te termina".
EMPUJE_ULTIMAS_HORAS = 6

# Cuántos compañeros hacen falta para que el número diga algo. El mismo piso que
# Intervalo: por debajo delata quién es y además no impresiona a nadie.
MIN_COMPANEROS = 5


@dataclass(frozen=True)
class Variante:
    key: str
    disponible: Callable[[dict], bool]
    render: Callable[[dict], tuple[str, str]]


def _mult(valor: float) -> str:
    """1.4 se escribe «1,4»: coma decimal, que es como se lee en castellano."""
    return f"{valor:.1f}".replace(".", ",")


# ── Social ───────────────────────────────────────────────────────────────────

def _social_hoy(ctx: dict) -> tuple[str, str]:
    return TITULO, (
        f"{ctx['companeros_hoy']} compañeros de la {ctx['universidad']} ya "
        f"derivaron hoy. ¿Vos? 🎓"
    )


def _social_semana(ctx: dict) -> tuple[str, str]:
    return TITULO, (
        f"La {ctx['universidad']} lleva {ctx['xp_universidad']} XP esta semana. "
        f"Sumá la tuya 🎓"
    )


def _social_aporte(ctx: dict) -> tuple[str, str]:
    return TITULO, (
        f"Sumaste {ctx['xp_propia']} XP para la {ctx['universidad']} esta "
        f"semana. ¿Seguimos? 🎓"
    )


# ── Reactivación ─────────────────────────────────────────────────────────────

def _reactivacion_ayer(ctx: dict) -> tuple[str, str]:
    return TITULO, "Ayer no derivaste. ¿Volvemos hoy? 👋"


def _reactivacion_dias(ctx: dict) -> tuple[str, str]:
    return TITULO, f"Hace {ctx['dias_inactivo']} días que no derivás. Te están pasando 👀"


# ── Récord ───────────────────────────────────────────────────────────────────

def _record_tanda(ctx: dict) -> tuple[str, str]:
    return TITULO, (
        f"Tu mejor tanda fueron {ctx['mejor_tanda']} derivadas seguidas. "
        f"¿La superás? 🚀"
    )


# ── Empuje (cafecito) ────────────────────────────────────────────────────────

def _empuje_termina(ctx: dict) -> tuple[str, str]:
    return TITULO, (
        f"Te quedan {ctx['empuje_horas']} h de ×{_mult(ctx['empuje_mult'])}. "
        f"¿Las aprovechás? ☕"
    )


def _empuje_nombrado(ctx: dict) -> tuple[str, str]:
    # Sin arroba: el nombre del donante viene del pago, no es un alias del juego.
    return TITULO, (
        f"{ctx['donante']} invitó un cafecito para la {ctx['universidad']}. "
        f"Tenés ×{_mult(ctx['empuje_mult'])} por {ctx['empuje_horas']} h ☕"
    )


def _empuje_anon(ctx: dict) -> tuple[str, str]:
    return TITULO, (
        f"Alguien de la {ctx['universidad']} invitó un cafecito. Tenés "
        f"×{_mult(ctx['empuje_mult'])} por {ctx['empuje_horas']} h ☕"
    )


def _empuje_global(ctx: dict) -> tuple[str, str]:
    return TITULO, (
        f"Hay un cafecito para todo el juego. Tenés "
        f"×{_mult(ctx['empuje_mult'])} por {ctx['empuje_horas']} h ☕"
    )


# ── Reclutas ─────────────────────────────────────────────────────────────────

def _recluta_primero(ctx: dict) -> tuple[str, str]:
    return TITULO, (
        f"Reclutaste a @{ctx['recluta_alias']} y ya te dio {ctx['recluta_xp']} XP 🪖"
    )


def _recluta_varios(ctx: dict) -> tuple[str, str]:
    return TITULO, (
        f"{ctx['reclutas']} de tus reclutas derivaron hoy: {ctx['recluta_xp']} "
        f"XP para vos 🪖"
    )


# ── Ranking ──────────────────────────────────────────────────────────────────

def _ranking_nombrado(ctx: dict) -> tuple[str, str]:
    return TITULO, f"@{ctx['rival_alias']} te pasó en el ranking. ¿Lo dejás así? 🤼"


def _ranking_generico(ctx: dict) -> tuple[str, str]:
    return TITULO, "Alguien te pasó en el ranking. ¿Lo dejás así? 🤼"


# ── Universidades ────────────────────────────────────────────────────────────

def _uni_paso(ctx: dict) -> tuple[str, str]:
    return TITULO, (
        f"La {ctx['universidad']} le pasó a la {ctx['rival_universidad']} en el "
        f"ranking 🏛️"
    )


def _uni_cerca(ctx: dict) -> tuple[str, str]:
    # Sin número, y no por pereza: la distancia en esta tabla es de Elo
    # promedio, no de XP, y ponerla en un aviso invita a "sumo XP para
    # defenderla" — que es exactamente lo que NO mueve este ranking. La frase es
    # la misma que el feed del juego ya usa para el mismo hecho.
    return TITULO, (
        f"La {ctx['rival_universidad']} está a nada de pasar a la "
        f"{ctx['universidad']}. ¿La defendés? 🏛️"
    )


def _hay(ctx: dict, *claves: str) -> bool:
    return all(ctx.get(k) is not None for k in claves)


VARIANTES: dict[str, list[Variante]] = {
    CAT_SOCIAL: [
        Variante(
            "social_hoy",
            lambda c: _hay(c, "universidad")
            and (c.get("companeros_hoy") or 0) > MIN_COMPANEROS,
            _social_hoy,
        ),
        Variante(
            "social_semana",
            lambda c: _hay(c, "universidad") and (c.get("xp_universidad") or 0) > 0,
            _social_semana,
        ),
        Variante(
            "social_aporte",
            lambda c: _hay(c, "universidad") and (c.get("xp_propia") or 0) > 0,
            _social_aporte,
        ),
    ],
    CAT_REACTIVACION: [
        Variante(
            "reactivacion_ayer",
            lambda c: c.get("dias_inactivo") == 1,
            _reactivacion_ayer,
        ),
        Variante(
            "reactivacion_dias",
            lambda c: (c.get("dias_inactivo") or 0) in DIAS_DE_REACTIVACION
            and (c.get("dias_inactivo") or 0) > 1,
            _reactivacion_dias,
        ),
    ],
    CAT_RECORD: [
        Variante("record_tanda", lambda c: (c.get("mejor_tanda") or 0) >= 3, _record_tanda),
    ],
    CAT_EMPUJE: [
        # El que se termina va PRIMERO y pide que no haya jugado hoy: es el único
        # que aporta algo que la persona no sabe. Si ya está jugando, avisarle
        # que tiene multiplicador es contarle lo que está viendo.
        Variante(
            "empuje_termina",
            lambda c: _hay(c, "universidad", "empuje_mult")
            and 0 < (c.get("empuje_horas") or 0) <= EMPUJE_ULTIMAS_HORAS
            and c.get("jugo_hoy") is False,
            _empuje_termina,
        ),
        Variante(
            "empuje_nombrado",
            lambda c: _hay(c, "universidad", "donante", "empuje_mult"),
            _empuje_nombrado,
        ),
        Variante(
            "empuje_anon",
            lambda c: _hay(c, "universidad", "empuje_mult") and not c.get("donante"),
            _empuje_anon,
        ),
        Variante(
            "empuje_global",
            lambda c: _hay(c, "empuje_mult") and not c.get("universidad"),
            _empuje_global,
        ),
    ],
    CAT_RECLUTA: [
        Variante(
            "recluta_primero",
            lambda c: bool(c.get("primer_recluta"))
            and _hay(c, "recluta_alias", "recluta_xp"),
            _recluta_primero,
        ),
        Variante(
            "recluta_varios",
            lambda c: _hay(c, "recluta_xp") and (c.get("reclutas") or 0) >= 1,
            _recluta_varios,
        ),
    ],
    CAT_RANKING: [
        Variante("ranking_nombrado", lambda c: _hay(c, "rival_alias"), _ranking_nombrado),
        Variante(
            "ranking_generico", lambda c: bool(c.get("perdio_puesto")), _ranking_generico
        ),
    ],
    CAT_UNIVERSIDAD: [
        Variante(
            "uni_paso",
            lambda c: bool(c.get("uni_paso"))
            and _hay(c, "universidad", "rival_universidad"),
            _uni_paso,
        ),
        Variante(
            "uni_cerca",
            lambda c: bool(c.get("uni_cerca"))
            and _hay(c, "universidad", "rival_universidad"),
            _uni_cerca,
        ),
    ],
}

# El orden en que se resuelven las reactivas. Sin sorteo: la primera que tenga
# hecho gana. Empuje primero porque vence —tiene reloj—, después lo que hizo otra
# persona por vos, y al final las dos comparaciones, que siguen siendo ciertas
# mañana.
ORDEN_REACTIVAS = (CAT_EMPUJE, CAT_RECLUTA, CAT_RANKING, CAT_UNIVERSIDAD)


def _disponibles(categoria: str, ctx: dict) -> list[Variante]:
    return [v for v in VARIANTES[categoria] if v.disponible(ctx)]


def categorias_disponibles(ctx: dict) -> set[str]:
    return {c for c in PESOS if _disponibles(c, ctx)}


def elegir_programada(
    *, contexto: dict, ultima_categoria: str | None, ultima_variante: str | None
) -> tuple[str, Variante] | None:
    """Categoría por sorteo pesado, evitando la de ayer, y variante adentro.

    Devuelve `None` cuando no hay nada que decir, que es lo normal: a diferencia
    de Intervalo —donde `practice` siempre tiene una variante genérica lista— acá
    todas las variantes piden un hecho. Preferimos no mandar nada antes que
    estrenar un «vení a jugar» sin motivo, que es la clase de aviso que hace que
    la gente apague el canal entero.
    """
    disponibles = categorias_disponibles(contexto)
    if not disponibles:
        return None

    pesos = {c: PESOS[c] for c in disponibles}
    if len(pesos) > 1 and ultima_categoria in pesos:
        sin_la_ultima = {c: p for c, p in pesos.items() if c != ultima_categoria}
        if sum(sin_la_ultima.values()) > 0:
            pesos = sin_la_ultima

    categorias = list(pesos.keys())
    categoria = random.choices(categorias, weights=[pesos[c] for c in categorias], k=1)[0]

    variantes = _disponibles(categoria, contexto)
    pool = [v for v in variantes if v.key != ultima_variante] or variantes
    return categoria, random.choice(pool)


def elegir_reactiva(categoria: str, contexto: dict) -> Variante | None:
    """La primera variante de esa categoría que tenga con qué. Sin sorteo: un
    aviso reactivo cuenta un hecho concreto y solo hay una forma de contarlo."""
    variantes = _disponibles(categoria, contexto)
    return variantes[0] if variantes else None
