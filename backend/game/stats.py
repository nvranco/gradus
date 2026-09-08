"""Estadísticas del jugador para el panel que abre la tecla `p` (ver
web/src/app/derivadas/elo-stats-panel.tsx y desktop-layout.tsx).

Dos paquetes en un solo viaje porque se muestran juntos: la card del ejercicio
se da vuelta y del otro lado va el Elo (histograma + explicación + generales),
y la del ranking se da vuelta y del otro lado va la MISMA tabla de derivadas
de siempre con dos columnas más (Elo de desbloqueo y accuracy personal).
Partirlo en dos pedidos sería separar algo que el producto trata como una
sola foto.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from models import GameAttempt, GameExercise, GamePlayer, GameTemplateStat

from . import elo
from .templates import TEMPLATE_BY_KEY

# A partir de cuántas derivadas RESUELTAS se desbloquea el panel. Mismo número
# que reclutas-trigger.ts (10), pero constante PROPIA y no un import cruzado:
# allá 10 es el resto de un contador periódico (10, 30, 50…); acá es un piso
# de una sola vez ("a partir de"). Comparten el número porque las dos leen
# "recién a las diez derivadas el juego empieza a hablarte de otra cosa", no
# porque sean el mismo mecanismo.
UMBRAL_ESTADISTICAS = 10

# Piso de jugadores CALIFICADOS para dibujar el histograma. Con menos, una
# campana de pocos puntos no es un gráfico: señala quiénes son y de paso
# miente sobre la forma real de la distribución. Mismo espíritu que
# boosts.MIN_PLAYERS_RANKED (10): no se muestra un número —acá, un dibujo—
# con poca base. Más alto que
# MIN_PLAYERS_RANKED a propósito: un promedio confiable pide menos evidencia
# que una FORMA confiable.
MIN_HISTOGRAM_PLAYERS = 20

# Buckets DINÁMICOS y no fijos. Cortar p̂ con bordes fijos se puede porque vive
# en [0,1] con una banda objetivo que los ancla (elo.TARGET_LOW / TARGET_HIGH).
# El rating no tiene ese ancla poblacional todavía —el juego lanzó el 24/8/2026,
# días antes de este archivo— así que un ancho fijo elegido ahora sería un
# número inventado. El día que haya semanas de historia real, se puede fijar.
N_BUCKETS = 16
BUCKET_WIDTH_MIN = 20  # puntos de rating; múltiplo al que se redondea el ancho

# Mínimo de intentos limpios (de la ventana de últimos 10) para animarse a
# mostrar un % de accuracy en una fila. Con 1 o 2 intentos un 100% o un 0% no
# dice nada: es la misma regla de «no mostrar un número sin base», por jugador.
MIN_MUESTRA_FILA = 3

# El θ que hay que sumarle a la β efectiva de una plantilla para que caiga en
# el CENTRO de la banda objetivo (p̂ = TARGET_MID). Es la misma cuenta con la
# que elo.py ya deriva _LEVEL_CUTS de BETA_SEED (ver su comentario: "θ ≈ β +
# logit(0.75)/SCALE = β + 1.34"), escrita una vez acá en vez de volver a
# hardcodear el 1.34.
_UNLOCK_THETA_OFFSET = math.log(elo.TARGET_MID / (1 - elo.TARGET_MID)) / elo.SCALE


def _unlock_theta(beta: float, tier: int, n_players: int) -> float:
    """El θ en el que ESTA plantilla empieza a sentirse cómoda.

    No es un gate real del juego —después de la rampa inicial
    (elo.RAMP_UPDATES=5) cualquier plantilla puede salir, ver
    generator.pick_template— así que "cuándo se desbloquea" hay que
    DEFINIRLO. Usa `effective_beta` y no la β cruda: es la misma corrección
    que ya usa el generador para elegir qué servir, así que este número
    cuenta la misma historia que la que el motor realmente sigue. Con
    n_players=0 (plantilla nunca vista), effective_beta ya devuelve la
    semilla del tier sin que haga falta un caso especial acá.
    """
    return elo.effective_beta(beta, tier, n_players) + _UNLOCK_THETA_OFFSET


# Mapeo EXACTO a las 14 filas de DerivativesTable (derivatives-table.tsx ::
# FILAS), mismo orden.
#
# Las tres filas que faltaban —1/x, √x y tan x— eran ciertas hasta el
# a4978533, que agregó `t1_recip`, `t1_sqrt` y `t3_tan` al final de TEMPLATES
# sin pasar por acá. Durante esos días el panel mostró «—» en tres de sus
# catorce filas y tiró a la basura los intentos de esas plantillas al armar la
# efectividad, porque no tenían dónde ir. Ya no hay ninguna fila sin plantilla:
# si mañana se agrega otra, esto se actualiza en el mismo commit y
# check_game_stats.py lo verifica.
ROW_TEMPLATES: dict[str, tuple[str, ...]] = {
    "a": ("t0_const",),
    "x": ("t0_x",),
    "x_n": ("t1_pow", "t1_kpow"),
    "inv_x": ("t1_recip",),
    "sqrt_x": ("t1_sqrt",),
    "e_x": ("t3_exp",),
    "a_x": ("t3_ax",),
    "ln_x": ("t3_ln",),
    "log_a_x": ("t3_loga",),
    "sin_x": ("t3_sin",),
    "cos_x": ("t3_cos",),
    "tan_x": ("t3_tan",),
    "prod": ("t4_pow_sin", "t4_pow_exp", "t4_exp_cos", "t4_pow_ln", "t4_exp_sin"),
    "quot": ("t5_sin_over_x", "t5_pow_over_linear", "t5_exp_over_pow",
             "t5_ln_over_x", "t5_linear_over_linear"),
}

# El reverso: de qué plantilla a qué fila visible. Las plantillas que NO
# aparecen acá (t1_kx, t2_sum2, t2_sum3, t2_pow_plus_const, t3_trig_sum,
# t3_mix_sum) se sirven pero no tienen fila propia — son combinaciones (suma,
# constante multiplicativa), mismo criterio que ya usa la tabla visual. Sus
# intentos se IGNORAN al armar el accuracy por fila: no hay dónde ponerlos.
_TEMPLATE_TO_SLUG: dict[str, str] = {
    key: slug for slug, keys in ROW_TEMPLATES.items() for key in keys
}


@dataclass
class _AccRow:
    accuracy: int | None
    sample: int
    # Milisegundos, promedio simple de la MISMA ventana que `accuracy` (los
    # últimos `sample` intentos limpios) — no es una velocidad aparte con su
    # propio recorte, es la otra lectura de la misma tanda de intentos.
    avg_response_ms: int | None


def _piso_de_fila(keys: tuple[str, ...]) -> int:
    """El rating a partir del cual el generador puede servir ALGO de esta fila.

    El mínimo y no el máximo: una fila se abre con su primera plantilla
    disponible. «Producto» tiene tres con seno o coseno y dos sin, así que
    sigue sin piso — x²·eˣ se puede servir desde siempre—, mientras que las
    filas de seno, coseno y tangente, que tienen una sola plantilla cada una,
    heredan el suyo entero.
    """
    return min((TEMPLATE_BY_KEY[k].min_rating or 0) for k in keys)


def _unlock_ratings(db: DBSession) -> dict[str, int | None]:
    """Elo de desbloqueo por fila (14 slugs). `None` solo si alguna fila se
    quedara sin plantilla — hoy no hay ninguna."""
    stats = {t.template_key: t for t in db.query(GameTemplateStat).all()}
    out: dict[str, int | None] = {}
    for slug, keys in ROW_TEMPLATES.items():
        if not keys:
            out[slug] = None
            continue
        thetas = []
        for key in keys:
            st = stats.get(key)
            tier = TEMPLATE_BY_KEY[key].tier
            beta = st.beta if st is not None else elo.BETA_SEED.get(tier, 0.0)
            n_players = st.n_players if st is not None else 0
            thetas.append(_unlock_theta(beta, tier, n_players))
        # Promedio en espacio θ y no de los ratings ya redondeados: promediar
        # números ya redondeados y volver a redondear acumula un sesgo que no
        # existe si se promedia antes de la única conversión.
        comodo = elo.rating_of(sum(thetas) / len(thetas))
        # El piso gana cuando hay uno: la β aprendida del seno lo daría por
        # cómodo en 870, y en 870 el generador no lo va a servir. Un panel que
        # promete una fila antes de que el motor la habilite miente en la única
        # pantalla donde el jugador va a buscar cuánto le falta.
        out[slug] = max(comodo, _piso_de_fila(keys))
    return out


def _personal_accuracy(db: DBSession, player_id: int) -> dict[str, _AccRow]:
    """Efectividad y velocidad PERSONALES de los últimos 10 intentos limpios,
    por fila.

    "Limpio" es la definición canónica del proyecto (metrics/game_queries.py
    :: _clean_firsts): primer intento, parse_ok, sin la tabla abierta. Se
    trae TODO el historial del jugador en una sola query ordenada por fecha
    (hay índice en player_id de las dos tablas) y se recorta a los últimos 10
    POR FAMILIA en Python — importa para las filas con varias plantillas
    (u·v, u/v), donde "últimos 10" es del conjunto de la familia entera y no
    10 de cada plantilla suelta.
    """
    filas = (
        db.query(
            GameAttempt.created_at,
            GameAttempt.is_correct,
            GameAttempt.response_ms,
            GameExercise.template_key,
        )
        .join(GameExercise, GameExercise.id == GameAttempt.exercise_id)
        .filter(
            GameAttempt.player_id == player_id,
            GameAttempt.attempt_number == 1,
            GameAttempt.parse_ok.is_(True),
            GameExercise.peeked.is_(False),
        )
        .order_by(GameAttempt.created_at.desc())
        .all()
    )
    ventanas: dict[str, list[tuple[bool, int | None]]] = defaultdict(list)
    for _created_at, is_correct, response_ms, template_key in filas:
        slug = _TEMPLATE_TO_SLUG.get(template_key)
        if slug is None:
            continue
        ventana = ventanas[slug]
        if len(ventana) < 10:
            ventana.append((bool(is_correct), response_ms))

    out: dict[str, _AccRow] = {}
    for slug in ROW_TEMPLATES:
        ventana = ventanas.get(slug, [])
        n = len(ventana)
        aciertos = sum(1 for correcto, _ in ventana if correcto)
        pct = round(100 * aciertos / n) if n >= MIN_MUESTRA_FILA else None
        # `response_ms` puede faltar (columna nullable): se promedia solo entre
        # los intentos que SÍ lo trajeron, y no se cuenta como un cero — un
        # tiempo ausente no es un tiempo instantáneo.
        tiempos = [ms for _, ms in ventana if ms is not None]
        avg_ms = round(sum(tiempos) / len(tiempos)) if len(tiempos) >= MIN_MUESTRA_FILA else None
        out[slug] = _AccRow(accuracy=pct, sample=n, avg_response_ms=avg_ms)
    return out


def _xp_de_los_reclutas(db: DBSession, player_id: int) -> int:
    """Cuánta XP le generaron a esta persona los que entraron por su link.

    Es la suma de `referral_xp_given` de sus RECLUTAS y no su propia columna.
    Esa columna guarda lo que ella le dio a quien la trajo —el otro lado de la
    relación— y durante un tiempo fue lo que mostraba el panel: quien reclutaba
    a diez personas veía un cero, y quien había entrado por el link de alguien
    veía lo que le venía pagando, rotulado como si fuera lo que había ganado.

    Es la misma cuenta que arma /leaderboard/recruits (router.py), del lado del
    total en vez de renglón por renglón. Sin filtrar por `exercises_correct`
    como allá: un recluta que no jugó tiene la columna en cero y no cambia la
    suma, y acá no hay ninguna lista que se llene de renglones vacíos.
    """
    total = (
        db.query(func.sum(GamePlayer.referral_xp_given))
        .filter(GamePlayer.referred_by == player_id)
        .scalar()
    )
    return total or 0


def _histograma(db: DBSession, player: GamePlayer) -> dict:
    """Dónde está el jugador respecto a la masa de jugadores CALIFICADOS.

    Calificado = is_bot=false Y exercises_correct >= UMBRAL_ESTADISTICAS — el
    mismo número y el mismo campo que pidió el usuario ("resolvieron 10 o más
    derivadas"), no elo.RAMP_UPDATES(=5): ese gobierna la rampa de tiers al
    servir, esto es una decisión de PRODUCTO nueva sobre qué jugador tiene
    juego suficiente para no ensuciar la campana con quien probó dos veces y
    se fue.
    """
    ratings = sorted(
        elo.rating_of(theta)
        for (theta,) in db.query(GamePlayer.theta)
        .filter(
            GamePlayer.is_bot.is_(False),
            GamePlayer.exercises_correct >= UMBRAL_ESTADISTICAS,
        )
        .all()
    )
    n = len(ratings)
    player_rating = elo.rating_of(player.theta)

    if n < MIN_HISTOGRAM_PLAYERS:
        return {
            "enough": False, "buckets": [], "n_players": n,
            "player_rating": player_rating, "player_bucket_index": None,
            "percentile": None,
        }

    lo, hi = ratings[0], ratings[-1]
    span = max(hi - lo, 1)
    ancho = max(BUCKET_WIDTH_MIN, round((span / N_BUCKETS) / BUCKET_WIDTH_MIN) * BUCKET_WIDTH_MIN)
    inicio = (lo // ancho) * ancho

    buckets = []
    borde = inicio
    while borde <= hi:
        buckets.append({"from_rating": borde, "to_rating": borde + ancho, "count": 0})
        borde += ancho
    if not buckets:  # todos con exactamente el mismo rating
        buckets = [{"from_rating": inicio, "to_rating": inicio + ancho, "count": 0}]

    def idx_de(rating: int) -> int:
        return min((rating - inicio) // ancho, len(buckets) - 1)

    for r in ratings:
        buckets[idx_de(r)]["count"] += 1

    mejor_que = sum(1 for r in ratings if r < player_rating)
    percentil = round(100 * mejor_que / (n - 1)) if n > 1 else None

    return {
        "enough": True, "buckets": buckets, "n_players": n,
        "player_rating": player_rating, "player_bucket_index": idx_de(player_rating),
        "percentile": percentil,
    }


def build(db: DBSession, player: GamePlayer) -> dict:
    """El payload entero de GET /game/derivemos/stats."""
    unlock = _unlock_ratings(db)
    personal = _personal_accuracy(db, player.id)
    rows = [
        {"slug": slug, "unlock_elo": unlock[slug],
         "accuracy": personal[slug].accuracy, "sample": personal[slug].sample,
         "avg_response_ms": personal[slug].avg_response_ms}
        for slug in ROW_TEMPLATES
    ]
    hist = _histograma(db, player)

    attempted = player.exercises_attempted
    accuracy_general = round(100 * player.exercises_correct / attempted) if attempted else None
    dias_jugando = max(0, (datetime.utcnow() - player.created_at).days)

    return {
        "n_rated_players": hist["n_players"],
        "enough_for_histogram": hist["enough"],
        "histogram": hist["buckets"],
        "player_rating": hist["player_rating"],
        "player_bucket_index": hist["player_bucket_index"],
        "percentile": hist["percentile"],
        "general": {
            "exercises_correct": player.exercises_correct,
            "exercises_attempted": player.exercises_attempted,
            "accuracy_overall": accuracy_general,
            "best_combo": player.best_combo,
            "best_rank": player.best_rank,
            "days_playing": dias_jugando,
            "xp": player.xp,
            # Cuánta XP le diste a tus reclutas por el link de WhatsApp — la
            # misma columna que ordena /leaderboard/recruits (router.py), acá
            # del lado de la propia persona.
            "xp_from_referrals": _xp_de_los_reclutas(db, player.id),
            # La XP extra que puso el empuje, acumulada al otorgar
            # (router.py :: _otorgar_xp). No se puede calcular acá: el
            # multiplicador de cada respuesta no queda guardado en ningún lado.
            "xp_from_boosts": player.xp_from_boosts,
        },
        "rows": rows,
    }
