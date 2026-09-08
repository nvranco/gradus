"""Empujes de XP por universidad, pagados con cafecitos.

Quien invita un cafecito multiplica el XP de TODA su universidad durante un día.
El giro a la universidad —en vez de un multiplicador personal— es lo que evita
que donar sea comprar puesto: si el empuje le toca a todos los de una
universidad, adentro de esa universidad el orden no se mueve, y lo que queda es
la pelea entre universidades.

El empuje ya NO es solo del minijuego: la misma tabla la lee Intervalo clásico
(ver backend/xp_boost.py), así que una donación hecha jugando a derivadas
multiplica también el XP de estudio de esa universidad, y al revés.

Reglas, todas acá:

  - Cada donación INSERTA una fila y ninguna se muta nunca. El multiplicador
    activo es una suma sobre las filas no vencidas, así que dos donaciones
    simultáneas no pueden pisarse y no hay estado que se corrompa.
  - Los cafecitos de la ventana SE SUMAN: diez personas con uno cada una llegan
    al mismo techo que una con diez. Eso es lo que hace que el slider del CTA
    sea un termómetro compartido ("faltan 3 para ×1,5") y no una compra
    individual.
  - El multiplicador se corta en MAX_MULTIPLIER. Sin tope, una sola persona con
    ganas dejaría el juego en ×5 y el XP dejaría de significar nada.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from models import GameBoost, GameBoostIntent, GamePlayer
from universities import UNIVERSITIES, canonical_university

from . import events, simulation

# Un día entero. Antes era media hora, elegida para que la cuenta regresiva del
# cartel generara urgencia ("quedan 12 minutos"); con el empuje valiendo también
# en Intervalo clásico esa urgencia dejó de ser lo que más importa. Lo que
# importa ahora es que el empuje alcance a la sesión de estudio de la persona,
# que puede caer en cualquier momento del día y no justo cuando alguien donó.
BOOST_HOURS = 24

# Los dos días son SOLO para quien llega al tope del multiplicador. Es el único
# escalón, y ese es el punto: una escala proporcional a los cafecitos (12, 24,
# 36…) hace que cada paso del slider mueva dos números a la vez, y con dos
# premios que crecen juntos ninguno de los dos se lee. Con un solo escalón, el
# slider tiene un lugar al que llegar.
BOOST_HOURS_MAX = 48

# Cada cafecito suma un décimo al multiplicador.
CAFECITO_STEP = 0.1

# El techo, que solo se alcanza ENTRE VARIOS.
MAX_MULTIPLIER = 3.0

# Lo que puede aportar UNA sola donación: +1,0, o sea que quien dona solo llega
# a ×2 y ahí se le termina la cuerda por más cafecitos que ponga. El ×3 no se
# compra, se junta — hacen falta al menos dos personas. Los cafecitos de más se
# guardan igual (la donación fue real y el feed la cuenta entera), pero no
# empujan el multiplicador.
MAX_CAFECITOS_PER_DONATION = 10

# Cuántos jugadores necesita una universidad para entrar al ranking per cápita.
# Con menos, un solo jugador afortunado la manda al tope y el ranking pasa a
# medir suerte en vez de universidades. Las que no llegan NO desaparecen: se
# devuelven con ranked=False y la UI las muestra al pie (ver router).
MIN_PLAYERS_RANKED = 10

# Cuánto vale una "voy a donar" antes de vencerse. Holgado a propósito: entre que
# alguien toca el botón, elige el monto en Cafecito y termina de pagar con Mercado
# Pago pueden pasar varios minutos.
INTENT_WINDOW_MINUTES = 30

# El prefijo de `external_ref` con el que firma cada canal de avisos: el socket
# de alertas de Cafecito (game/cafecito_stream.py) y el mail de Mercado Pago
# reenviado (game/cafecito_email.py).
FUENTE_SOCKET = "cafecito:"
FUENTE_MAIL = "mp:"

# Cuánto puede pasar entre los dos avisos de una MISMA donación.
#
# Los dos llegan en segundos y no comparten ningún identificador: el socket trae
# el nombre que la persona escribió en el formulario, el mail trae un número de
# operación de Mercado Pago. Lo único que tienen en común es cuánto y cuándo, así
# que eso es lo que se compara (ver `aviso_repetido`).
#
# Tres minutos es holgado para la diferencia entre los dos avisos y corto para lo
# otro que puede pasar: que dos personas donen la misma cantidad casi juntas. Si
# eso ocurre, la segunda no se aplica — y por eso el que descarta lo grita en el
# log, que es lo que permite repararlo después con grant_game_boost.py.
VENTANA_MISMO_PAGO_S = 180

# El `source` de los empujes por aforo. Está escrito acá y no importado de
# game/aforo.py porque aforo importa boosts: traerlo al revés cierra el ciclo.
# El check verifica que los dos digan lo mismo.
_SOURCE_AFORO = "aforo"


def horas_de(cafecitos: int) -> int:
    """Cuánto dura el empuje de UNA donación.

    Un día, salvo que la donación llegue al tope del multiplicador: ahí, y solo
    ahí, dos. El espejo en el front es `horasDe` de
    web/src/app/derivadas/cafecito-panel.tsx, que es lo que dibuja el slider.
    """
    return BOOST_HOURS_MAX if cafecitos >= MAX_CAFECITOS_PER_DONATION else BOOST_HOURS


# Siglas del catálogo, para reconocerlas dentro del mensaje de una donación.
_KNOWN_SIGLAS = {sigla for sigla, _ in UNIVERSITIES}


@dataclass(frozen=True)
class BoostView:
    """Un empuje vigente, ya agregado por universidad. `university=None` = global.

    `multiplier` y `cafecitos` NO son dos vistas del mismo número: el primero ya
    tiene aplicado el tope por donación (fila por fila, ver `_CAPPED`) y el
    segundo es el total crudo. Con una donación de 30 valen ×2,0 y 30. Mezclarlos
    hacía que el chip prometiera un multiplicador que la respuesta no pagaba.
    """

    university: str | None
    multiplier: float
    cafecitos: int
    donor_name: str | None
    expires_in_seconds: int
    # Parte de este multiplicador la puso el aforo del día (game/aforo.py) y no
    # una donación. El cartel lo necesita para no decir "5 cafecitos" de un
    # empuje que nadie pagó: `cafecitos` cuenta SOLO los donados, así que sin
    # esta bandera un empuje de aforo puro se anunciaría como cero cafecitos.
    aforo: bool = False


def multiplier_from_cafecitos(cafecitos: int) -> float:
    if cafecitos <= 0:
        return 1.0
    return min(MAX_MULTIPLIER, 1.0 + cafecitos * CAFECITO_STEP)


def _now() -> datetime:
    # utcnow naive, igual que el resto del proyecto (models.py, simulation.py).
    return datetime.utcnow()


# Casi todo el tiempo NO hay ningún empuje vigente, y averiguarlo cuesta caro en
# los dos caminos más calientes del juego: tres consultas en cada respuesta
# correcta (global + candado de mudanza + la de la universidad) y una más en cada
# pulso, que cada pestaña abierta pide cada diez segundos.
#
# El atajo es memorizar el NO. Si no hay ni una fila vigente, no puede aparecer
# ninguna sin que alguien llame a `grant`, así que se puede sostener esa
# respuesta unos segundos sin volver a preguntar. `grant` limpia la memoria al
# terminar, y por eso una donación se siente en el acto y no cuando vence el TTL.
#
# El SÍ TAMBIÉN se memoriza, con el mismo TTL, y eso cambió con la duración.
# Cuando un empuje duraba treinta minutos, "hay uno" era el caso raro y valía
# pagar las consultas completas; con `BOOST_HOURS = 24` el caso raro dura uno o
# dos días enteros, y durante todo ese tiempo CADA respuesta de Intervalo clásico
# y cada pulso del juego pagaban las tres consultas otra vez.
#
# Lo que se memoriza es solo la EXISTENCIA, no los montos: el multiplicador se
# sigue calculando contra la base en cada llamada. Así una donación que entra en
# el medio se cobra completa desde el primer instante, y el único error posible
# son cinco segundos de "ya no hay ninguno" cuando el último acaba de vencer —
# exactamente el mismo margen que ya se aceptaba del otro lado.
#
# Es caché por proceso: con varios workers cada uno tiene el suyo, y lo peor que
# pasa es que uno tarde unos segundos de más en enterarse.
_EMPUJES_TTL_SEGUNDOS = 5.0
_empujes_hasta = 0.0
_habia_empujes = False


def olvidar_cache_de_empujes() -> None:
    """Fuerza a la próxima consulta a volver a mirar la base."""
    global _empujes_hasta
    _empujes_hasta = 0.0


def hay_empujes(db: Session, now: datetime) -> bool:
    global _empujes_hasta, _habia_empujes
    if time.monotonic() < _empujes_hasta:
        return _habia_empujes
    _habia_empujes = (
        db.query(GameBoost.id).filter(GameBoost.expires_at > now).first() is not None
    )
    _empujes_hasta = time.monotonic() + _EMPUJES_TTL_SEGUNDOS
    return _habia_empujes


# El tope por donación se aplica FILA POR FILA y no sobre la suma: si se topeara
# el total, dos personas de a 10 llegarían al mismo lugar que una sola de 20, y
# la regla de "hay que colaborar" no existiría.
#
# Con `case` y no con la función `min`/`least` de la base: SQLite y Postgres las
# escriben distinto y esto corre en las dos.
_CAPPED = case(
    (GameBoost.cafecitos > MAX_CAFECITOS_PER_DONATION, MAX_CAFECITOS_PER_DONATION),
    else_=GameBoost.cafecitos,
)


def _cafecitos(db: Session, where, now: datetime) -> int:
    return int(
        db.query(func.coalesce(func.sum(_CAPPED), 0))
        .filter(where, GameBoost.expires_at > now)
        .scalar()
        or 0
    )


def global_cafecitos(db: Session, now: datetime | None = None) -> int:
    """Los del empuje global (university IS NULL), que le tocan a todo el mundo."""
    return _cafecitos(db, GameBoost.university.is_(None), now or _now())


def cafecitos_de(db: Session, university: str, now: datetime | None = None) -> int:
    """Los dirigidos a UNA universidad, ya topeados fila por fila.

    Pública porque Intervalo clásico arma el mismo total desde otra tabla (ver
    backend/xp_boost.py) y no puede depender de `_cafecitos`, que es el detalle
    de cómo se aplica el tope."""
    return _cafecitos(db, GameBoost.university == university, now or _now())


def multiplier_desde(
    db: Session,
    university: str | None,
    set_at: datetime | None,
    now: datetime | None = None,
) -> float:
    """El reparto del empuje, una sola vez y para los dos productos.

    Lo global entra SIEMPRE, incluso para quien no cargó universidad: es un
    regalo para todos, y dejar afuera justo al que todavía no eligió sería al
    revés de lo que se busca. Lo dirigido se suma solo si el candado antimudanza
    lo deja pasar.

    Toma los dos campos sueltos y no una fila, igual que `aplica_el_empuje` y por
    el mismo motivo: en el minijuego salen de `game_players`, en Intervalo
    clásico de `enrollments`. Lo que cambia entre los dos productos es de DÓNDE
    salen los campos; el reparto es el mismo, y estaba escrito tres veces —acá,
    en `multiplier_for_player` y en `xp_boost.multiplier_for_user`—. No divergían
    todavía; divergen al primer cambio, y `multiplier_for`, que alimenta el feed
    de eventos, es la más fácil de olvidar.

    `set_at=None` saltea el candado, que es lo correcto para quien pregunta por
    una universidad y no por una persona (el feed, el chip del cartel): ahí no
    hay nadie que se haya podido mudar.
    """
    now = now or _now()
    if not hay_empujes(db, now):
        return 1.0
    total = global_cafecitos(db, now)
    if university and aplica_el_empuje(university, set_at, db, now):
        total += _cafecitos(db, GameBoost.university == university, now)
    return multiplier_from_cafecitos(total)


def multiplier_for(db: Session, university: str | None, now: datetime | None = None) -> float:
    """Multiplicador vigente de una universidad, sin mirar a ninguna persona."""
    return multiplier_desde(db, university, None, now)


def aplica_el_empuje(
    university: str | None,
    set_at: datetime | None,
    db: Session,
    now: datetime | None = None,
) -> bool:
    """¿Le corresponde el empuje de esa universidad a quien se mudó en `set_at`?

    No, si se cambió de universidad DESPUÉS de que arrancara el empuje más viejo
    que sigue vigente. Sin este candado, cada empuje se llenaría de gente que se
    muda por un día a la universidad impulsada y la rivalidad entre universidades
    —que es todo el punto— se muere en una tarde.

    Cargar la universidad por primera vez no cuenta como mudarse: el sello queda
    en NULL en ese caso, así que quien recién se suma y elige su universidad
    cobra desde el primer momento.

    Toma los dos campos sueltos y no una fila para que la usen los DOS productos:
    en el minijuego salen de `game_players`, en Intervalo clásico de
    `enrollments` (ver backend/xp_boost.py). El candado tiene que ser el mismo
    en los dos lados o mudarse por un lado paga el empuje del otro.
    """
    if not university or set_at is None:
        return True
    now = now or _now()
    # Solo los DIRIGIDOS a esa universidad: un empuje global no tiene a dónde
    # mudarse, así que el candado no le aplica.
    first_started = (
        db.query(func.min(GameBoost.created_at))
        .filter(GameBoost.university == university, GameBoost.expires_at > now)
        .scalar()
    )
    if first_started is None:
        return True
    return set_at < first_started


def applies_to(player: GamePlayer, db: Session, now: datetime | None = None) -> bool:
    """El candado, para un jugador del minijuego. Ver `aplica_el_empuje`."""
    return aplica_el_empuje(player.university, player.university_set_at, db, now)


def multiplier_for_player(
    db: Session, player: GamePlayer, now: datetime | None = None
) -> float:
    """El multiplicador de un jugador del minijuego. Ver `multiplier_desde`."""
    return multiplier_desde(db, player.university, player.university_set_at, now)


def active_boosts(db: Session, now: datetime | None = None) -> list[BoostView]:
    """Empujes vigentes agregados por universidad, del más fuerte al más flojo.

    El nombre que se muestra es el de la donación MÁS RECIENTE de esa universidad:
    con varias sumadas hay que elegir una, y la última es la que la persona
    acaba de hacer — es la que espera ver en pantalla.
    """
    now = now or _now()
    if not hay_empujes(db, now):
        return []
    rows = (
        db.query(GameBoost)
        .filter(GameBoost.expires_at > now)
        .order_by(GameBoost.created_at.asc())
        .all()
    )
    by_uni: dict[str | None, dict] = {}
    for row in rows:
        agg = by_uni.setdefault(
            row.university,
            {"cafecitos": 0, "topeados": 0, "donor_name": None,
             "aforo": False, "expires_at": row.expires_at},
        )
        # DOS sumas, y la diferencia entre las dos es el bug que esto arregla.
        #
        # `cafecitos` es el total crudo, que es lo que el cartel cuenta y lo que
        # boosts.py sanciona más arriba: la donación fue real y el feed la cuenta
        # entera. Pero el MULTIPLICADOR no se calcula con eso, porque el tope por
        # donación se aplica fila por fila (ver `_CAPPED`), así que una donación
        # de 30 anunciaba ×3,0 mientras la respuesta pagaba ×2,0 — y el feed, que
        # sale de `multiplier_for`, decía ×2,0 al mismo tiempo que el chip decía
        # ×3,0.
        # `cafecitos` cuenta lo DONADO y `topeados` lo que empuja el
        # multiplicador. El empuje por aforo entra en el segundo y no en el
        # primero: vale ×0,5 igual que cinco cafecitos, pero nadie los invitó y
        # el cartel no puede decir que sí.
        es_aforo = row.source == _SOURCE_AFORO
        if es_aforo:
            agg["aforo"] = True
        else:
            agg["cafecitos"] += row.cafecitos
        agg["topeados"] += min(row.cafecitos, MAX_CAFECITOS_PER_DONATION)
        if row.donor_name:
            agg["donor_name"] = row.donor_name
        # El cartel muestra un solo reloj por universidad: el del empuje que dura más.
        agg["expires_at"] = max(agg["expires_at"], row.expires_at)

    views = [
        BoostView(
            university=uni,
            multiplier=multiplier_from_cafecitos(agg["topeados"]),
            cafecitos=agg["cafecitos"],
            donor_name=agg["donor_name"],
            aforo=agg["aforo"],
            expires_in_seconds=max(0, int((agg["expires_at"] - now).total_seconds())),
        )
        for uni, agg in by_uni.items()
    ]
    # El global primero: le toca a todos, así que es la noticia más grande del
    # cartel aunque su multiplicador sea más chico que el de alguna universidad.
    views.sort(key=lambda v: (v.university is None, v.multiplier, v.expires_in_seconds),
               reverse=True)
    return views


def grant(
    db: Session,
    university: str | None,
    cafecitos: int,
    donor_name: str | None = None,
    source: str = "manual",
    external_ref: str | None = None,
    minutes: int | None = None,
    now: datetime | None = None,
    anunciar: bool = True,
) -> GameBoost | None:
    """Registra un empuje. Devuelve None si `external_ref` ya se usó.

    `anunciar=False` inserta la fila y mueve el pulso, pero no emite el evento
    de "invitó cafecitos" al feed. Lo usa el empuje por aforo (game/aforo.py),
    que no lo invitó nadie y tiene su propia frase: sin esta salida el feed
    anunciaría una donación de cinco cafecitos que no existe.

    La sigla se canonicaliza acá y no en el que llama: el empuje tiene que
    matchear `game_players.university` exactamente, y esa columna guarda lo que
    devuelve `canonical_university`.
    """
    if cafecitos <= 0:
        raise ValueError("cafecitos tiene que ser mayor que cero")
    # None = empuje GLOBAL. Un string vacío, en cambio, es un error de quien
    # llama: quiso decir una universidad y no le salió.
    uni = None
    if university is not None:
        uni = (canonical_university(university) or "").strip() or None
        if uni is None:
            raise ValueError("universidad vacía")

    if external_ref is not None:
        already = (
            db.query(GameBoost).filter(GameBoost.external_ref == external_ref).first()
        )
        if already is not None:
            return None

    now = now or _now()
    # Dos unidades, y por eso la rama explícita: la duración normal se cuenta en
    # HORAS, pero `minutes` sigue existiendo como override de operaciones
    # (grant_game_boost.py --minutes, que es como se prueba un empuje corto sin
    # esperar un día). Colapsar las dos en una sola cuenta convertía un
    # `--minutes 3` en un empuje de 24 h.
    dura = timedelta(minutes=minutes) if minutes is not None else timedelta(hours=horas_de(cafecitos))
    boost = GameBoost(
        university=uni,
        cafecitos=cafecitos,
        donor_name=(donor_name or None),
        source=source,
        external_ref=external_ref,
        created_at=now,
        expires_at=now + dura,
    )
    db.add(boost)
    # El evento sale con el multiplicador YA acumulado, no con el de esta
    # donación sola: si hay otras vigentes, lo que la gente ve en el cartel es la
    # suma, y el feed tiene que decir lo mismo.
    db.flush()
    # Antes de cualquier lectura: si venía memorizado que no había empujes, el
    # multiplicador del evento de acá abajo saldría en ×1 y el cartel anunciaría
    # una donación que no cambió nada.
    olvidar_cache_de_empujes()
    if anunciar:
        events.on_boost(
            db,
            university=uni,
            cafecitos=cafecitos,
            multiplier=multiplier_for(db, uni, now=now),
            donor_name=donor_name,
        )
    # El ranking va a moverse distinto a partir de ahora: que el pulso avise.
    simulation.bump_version(db)
    return boost


# --- de la donación al empuje ----------------------------------------------

# Desde cuándo una intención ya no es "acabo de volver de Cafecito" sino algo que
# pasó en otra sesión. Más larga que INTENT_WINDOW_MINUTES a propósito: pasada la
# ventana la donación ya no se puede emparejar, pero la persona sigue mereciendo
# que le digamos que no llegó en vez de no decirle nada.
MEMORIA_INTENCION_HORAS = 6


@dataclass(frozen=True)
class EstadoDonacion:
    """Lo que hay para contarle a quien volvió de Cafecito."""

    state: str  # "none" | "pending" | "credited"
    university: str | None = None
    cafecitos: int = 0
    multiplier: float = 1.0
    expires_in_seconds: int = 0


def estado_de_donacion(
    db: Session, player: GamePlayer, now: datetime | None = None
) -> EstadoDonacion:
    """¿Qué pasó con el cafecito de esta persona?

    Se mira su última intención. `consumed_at` es la señal, y es exacta: la
    donación que llega marca como cumplidas todas las intenciones abiertas, con
    universidad o sin ella (ver `resolve_donation`).

    El empuje que se reporta es el que se creó junto con esa marca. Se busca por
    tiempo y no por una clave que los una porque una misma donación puede crear
    VARIAS filas —si dos universidades donaron a la vez cobran las dos— y lo que
    esta persona tiene que ver es la suya.
    """
    now = now or _now()
    intent = (
        db.query(GameBoostIntent)
        .filter(
            GameBoostIntent.player_id == player.id,
            GameBoostIntent.created_at > now - timedelta(hours=MEMORIA_INTENCION_HORAS),
        )
        .order_by(GameBoostIntent.created_at.desc())
        .first()
    )
    if intent is None:
        return EstadoDonacion(state="none")
    if intent.consumed_at is None:
        return EstadoDonacion(state="pending")

    # ¿Se puede afirmar que esta donación fue de ESTA persona?
    #
    # Cafecito no dice quién donó, así que la intención es una apuesta: la
    # donación que llega marca como cumplidas TODAS las abiertas, y si había
    # varias, cualquiera de ellas pudo haber sido. Repartir el empuje entre todas
    # está bien —equivocarse ahí es barato, en el peor caso una universidad
    # recibe un regalo— pero decirle «llegó tu cafecito» a alguien que no pagó es
    # caro: es la única frase del juego que puede hacer sentir estafado a quien
    # la lee.
    #
    # Las intenciones que consumió una misma donación comparten el instante
    # exacto, así que contarlas alcanza para saber si hay ambigüedad. Con más de
    # una, se muestra «todavía no llegó», que es lo honesto: no lo sabemos. Quien
    # sí pagó igual va a ver su empuje en las novedades.
    hermanas = (
        db.query(GameBoostIntent)
        .filter(GameBoostIntent.consumed_at == intent.consumed_at)
        .count()
    )
    if hermanas > 1:
        return EstadoDonacion(state="pending")

    # El empuje que nació con esa marca. `grant` y el consumo comparten el mismo
    # instante, pero se busca con un margen por si alguna vez dejan de hacerlo.
    margen = timedelta(seconds=5)
    candidatos = (
        db.query(GameBoost)
        .filter(
            GameBoost.created_at >= intent.consumed_at - margen,
            GameBoost.created_at <= intent.consumed_at + margen,
        )
        .order_by(GameBoost.id)
        .all()
    )
    # El de su universidad si está; si no, el global, que es donde cae la
    # donación de quien todavía no eligió.
    suyo = next((b for b in candidatos if b.university == intent.university), None)
    if suyo is None:
        suyo = next((b for b in candidatos if b.university is None), None)
    if suyo is None:
        # Se consumió pero no encontramos el empuje. No debería pasar; ante la
        # duda se dice que llegó, que es lo cierto, sin los detalles.
        return EstadoDonacion(state="credited", multiplier=multiplier_for_player(db, player, now))

    restante = max(0, int((suyo.expires_at - now).total_seconds()))
    return EstadoDonacion(
        state="credited" if restante > 0 else "none",
        university=suyo.university,
        cafecitos=suyo.cafecitos,
        multiplier=multiplier_for_player(db, player, now),
        expires_in_seconds=restante,
    )


def record_intent(
    db: Session,
    player: GamePlayer,
    now: datetime | None = None,
    university: str | None = None,
) -> GameBoostIntent:
    """Anota que este jugador se va a Cafecito. Es la pata que no le pide nada al
    donante: acá el juego todavía sabe quién es y de qué universidad.

    `university` deja pasar una universidad de otra fuente para el caso en que el
    jugador no tenga: quien abre la diapo desde Intervalo clásico puede tener un
    `GamePlayer` recién creado y sin universidad, pero un enrollment que sí la
    sabe (ver `router.cafecito_intent`, el único que la manda). Omitida, se usa
    la del jugador, que es lo de siempre.
    """
    intent = GameBoostIntent(
        player_id=player.id,
        university=university if university is not None else player.university,
        created_at=now or _now(),
    )
    db.add(intent)
    db.flush()
    return intent


def _intents_abiertas(db: Session, now: datetime) -> list[GameBoostIntent]:
    """TODAS las intenciones vigentes, tengan universidad o no."""
    return (
        db.query(GameBoostIntent)
        .filter(
            GameBoostIntent.consumed_at.is_(None),
            GameBoostIntent.created_at > now - timedelta(minutes=INTENT_WINDOW_MINUTES),
        )
        .order_by(GameBoostIntent.created_at.desc())
        .all()
    )


def pending_intents(db: Session, now: datetime | None = None) -> list[GameBoostIntent]:
    """Las que sirven para elegir DESTINO: solo las que tienen universidad.

    Una intención sin universidad no aporta a dónde mandar el empuje —de eso se
    encarga el escalón global— pero sí se marca como cumplida cuando llega una
    donación (ver `resolve_donation`), porque de eso depende poder decirle a la
    persona que su cafecito llegó.
    """
    return [i for i in _intents_abiertas(db, now or _now()) if i.university]


def universities_in_text(*textos: str | None) -> list[str]:
    """Las siglas del catálogo que aparezcan en cualquiera de estos textos.

    Se parte en palabras y cada una se pasa por el catálogo, en vez de buscar
    siglas con una regex: "UNT" también aparece adentro de otras palabras, y
    `canonical_university` ya acepta mayúsculas, minúsculas y el nombre completo.

    Se mira el NOMBRE y no solo el mensaje, y esto no es una mejora teórica: lo
    enseñó una donación real. Alguien puso «Santi ITBA» de nombre, «Muy bueno!»
    de mensaje, y donó diez cafecitos —el tope que aporta una persona—. Como la
    sigla no estaba en el mensaje, el empuje se fue al escalón siguiente y lo
    cobró otra universidad. El parser habría encontrado el ITBA; nadie le
    preguntó por el nombre.

    El supuesto que se había colado era que quien quiere dirigir su cafecito lo
    escribe en el mensaje. Pero el nombre es el primer campo del formulario y es
    donde uno pone quién es — y para un estudiante, parte de quién es, es dónde
    estudia.
    """
    encontradas: list[str] = []
    for texto in textos:
        if not texto:
            continue
        for palabra in re.findall(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", texto):
            uni = canonical_university(palabra)
            # `canonical_university` devuelve el texto tal cual cuando no lo
            # conoce, así que solo vale si lo que salió es una sigla DEL catálogo.
            if uni and uni in _KNOWN_SIGLAS and uni not in encontradas:
                encontradas.append(uni)
    return encontradas


def universities_in_message(message: str | None) -> list[str]:
    """Compatibilidad: solo el mensaje. Ver `universities_in_text`."""
    return universities_in_text(message)


def aviso_repetido(
    db: Session,
    cafecitos: int,
    fuente: str,
    now: datetime | None = None,
) -> bool:
    """Esta misma donación, ¿ya entró por el OTRO canal?

    Existe porque las dos vías son independientes a propósito —cada una tapa el
    agujero de la otra— y por lo tanto la mayoría de las donaciones se anuncian
    dos veces. Sin esto, cada cafecito valdría el doble de lo que promete el
    slider, y el termómetro compartido del CTA («faltan 3 para ×1,5») dejaría de
    querer decir algo.

    Solo mira los avisos de las OTRAS fuentes, nunca los de la propia: dos
    eventos del mismo canal ya los deduplica cada canal a su manera, y mezclarlo
    haría que una donación legítima de la misma cantidad se pierda por venir
    detrás de otra.
    """
    otras = [p for p in (FUENTE_SOCKET, FUENTE_MAIL) if p != fuente]
    if not otras:
        return False
    desde = (now or _now()) - timedelta(seconds=VENTANA_MISMO_PAGO_S)
    return (
        db.query(GameBoost.id)
        .filter(
            GameBoost.cafecitos == cafecitos,
            GameBoost.created_at > desde,
            or_(*[GameBoost.external_ref.like(f"{p}%") for p in otras]),
        )
        .first()
        is not None
    )


def resolve_donation(
    db: Session,
    cafecitos: int,
    donor_name: str | None = None,
    message: str | None = None,
    external_ref: str | None = None,
    minutes: int | None = None,
    now: datetime | None = None,
) -> list[GameBoost]:
    """Convierte una donación en uno o más empujes. Nunca devuelve lista vacía.

    La escalera, de más a menos información:

      1. Hay siglas en el mensaje  → esas universidades, más las intenciones
         abiertas (si dos personas donaron a la vez, cobran las dos).
      2. No hay siglas, pero sí intenciones → todas esas universidades.
      3. No hay nada → empuje GLOBAL, para todo el mundo.

    La regla que ordena todo esto es que una donación **nunca** puede terminar en
    nada: quien pagó tiene que ver algo pasar. Ante la duda se reparte de más —en
    el peor caso le regalamos el empuje a una universidad que no donó, que no le
    hace mal a nadie— antes que arriesgarse a no darle nada a quien sí donó.
    """
    now = now or _now()
    if external_ref is not None:
        ya = db.query(GameBoost).filter(GameBoost.external_ref == external_ref).first()
        if ya is not None:
            return []

    abiertas = _intents_abiertas(db, now)
    # El nombre cuenta igual que el mensaje: ver `universities_in_text`.
    destinos: list[str] = list(universities_in_text(message, donor_name))
    for i in abiertas:
        if i.university and i.university not in destinos:
            destinos.append(i.university)
    # Se marcan TODAS las abiertas, también las que no tienen universidad.
    #
    # Para elegir destino esas no aportan nada —su donación cae en el escalón
    # global— pero `consumed_at` es además lo único con lo que el juego puede
    # decirle a quien volvió de Cafecito «tu cafecito llegó». Dejándolas sin
    # marcar, a quien donó sin haber elegido universidad se le quedaba mostrando
    # «estamos esperando que se acredite» para siempre, que es exactamente lo que
    # no puede pasarle a alguien que acaba de pagar.
    for i in abiertas:
        i.consumed_at = now

    # `external_ref` es UNIQUE, así que con varios destinos solo el primero puede
    # llevarlo. Alcanza: es la fila que hace que un mail repetido no entre dos
    # veces, y las demás se crean o no junto con ella en la misma transacción.
    creados: list[GameBoost] = []
    for n, destino in enumerate(destinos or [None]):
        boost = grant(
            db,
            university=destino,
            cafecitos=cafecitos,
            donor_name=donor_name,
            source="cafecito" if external_ref else "manual",
            external_ref=external_ref if n == 0 else None,
            minutes=minutes,
            now=now,
        )
        if boost is not None:
            creados.append(boost)
    return creados
