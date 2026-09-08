"""Historial de eventos del juego — qué merece ser noticia y cómo se cuenta.

El feed que vive debajo del CTA es SOLO del sistema: ninguna línea la escribe un
usuario, así que no hay nada que moderar. Lo que sí hay que cuidar es el ruido,
y de eso se ocupan tres cosas:

  · **Umbrales.** Una escalada de un puesto no es noticia; una de cinco sí. Una
    racha de tres tampoco; una de diez sí.
  · **`dedupe_key`.** El hito de racha 25 de alguien se cuenta UNA vez, no en
    cada respuesta que lo mantenga. Lo mismo el registro, o el aviso de que una
    universidad viene pisándole los talones a otra.
  · **Solo gente real.** Los jugadores sembrados mueven el ranking (ver
    simulation.py) pero no generan eventos con nombre y apellido: que un número
    suba es una cosa, y afirmar "@fulano pasó a doce personas" cuando @fulano no
    existe es otra bastante peor. Los eventos de UNIVERSIDAD sí los incluyen,
    porque ahí lo que se afirma es un agregado, que es cierto.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import GameEvent, GamePlayer, GameSimState
from universities import article_for

from . import elo, ranking

# Cuántas líneas trae el feed de arranque: lo que llena la primera pantalla con
# margen para scrollear un poco antes de tener que pedir más.
FEED_LIMIT = 40

# Y cuánto se puede pedir de una sola vez. Es el techo de `limit`, no su valor:
# el panel pide de a poco y para atrás (ver `before_id`), pero nada de eso puede
# terminar en un `SELECT` sin freno si algún día alguien manda `limit=100000`.
MAX_LIMIT = 100

# Puestos ganados de una sola respuesta para que la escalada sea noticia.
CLIMB_MIN = 5

# Hitos de racha que se cuentan. No es "cada 5": una racha de 5 la tiene
# cualquiera, y el feed se llenaría de rachas.
STREAK_MILESTONES = (10, 25, 50, 100, 250)

# Dos universidades "se vienen pisando" cuando las separa menos de esto, en
# proporción del promedio de la de adelante.
UNI_CLOSE_RATIO = 0.02

# Cada cuánto puede repetirse el aviso de que una universidad viene atrás de otra.
# Sin esta ventana, dos universidades parejas empujarían un aviso por tick.
UNI_CLOSE_COOLDOWN_MINUTES = 30

# Los eventos viejos no se muestran ni sirven; se barren en el mismo tick que
# mueve la simulación.
PRUNE_DAYS = 7

EMOJI = {
    "boost": "☕",
    "signup": "🎓",
    "referral": "🪖",
    "climb": "🚀",
    "streak": "🔥",
    "lead": "👑",
    "level": "⚡",
    "uni_pass": "🏛️",
    "uni_close": "👀",
}


@dataclass(frozen=True)
class EventView:
    id: int
    kind: str
    text: str
    emoji: str
    actor_alias: str | None
    actor_level: int | None
    actor_b_alias: str | None
    universities: list[str]
    university: str | None
    university_b: str | None
    player_id: int | None
    seconds_ago: int


def _now() -> datetime:
    return datetime.utcnow()


def emit(
    db: Session,
    kind: str,
    text: str,
    actor_alias: str | None = None,
    actor_level: int | None = None,
    actor_b_alias: str | None = None,
    player_id: int | None = None,
    university: str | None = None,
    university_b: str | None = None,
    dedupe_key: str | None = None,
    dedupe_minutes: int | None = None,
    now: datetime | None = None,
) -> GameEvent | None:
    """Registra un evento. Devuelve None si la clave de deduplicación ya se usó.

    `text` viene con marcadores en vez de con los nombres puestos: `{a}` para el
    protagonista, `{b}` para un segundo protagonista (si lo hay) y `{u0}`/`{u1}`
    para las siglas. El cliente los reemplaza por la tag de la universidad y por
    el nombre pintado con el color de su nivel — con la oración ya resuelta eso
    no se puede hacer sin adivinar dónde empieza cada cosa.

    `dedupe_minutes=None` significa "una sola vez para siempre" (un registro, un
    hito de racha). Con un número, la clave se puede volver a usar pasada esa
    ventana (una universidad que vuelve a acercarse a otra).
    """
    now = now or _now()
    if dedupe_key is not None:
        q = db.query(GameEvent.id).filter(GameEvent.dedupe_key == dedupe_key)
        if dedupe_minutes is not None:
            q = q.filter(GameEvent.created_at > now - timedelta(minutes=dedupe_minutes))
        if q.first() is not None:
            return None

    event = GameEvent(
        kind=kind,
        text=text,
        emoji=EMOJI.get(kind, "•"),
        actor_alias=actor_alias,
        actor_level=actor_level,
        actor_b_alias=actor_b_alias,
        player_id=player_id,
        university=university,
        university_b=university_b,
        dedupe_key=dedupe_key,
        created_at=now,
    )
    db.add(event)
    # El flush es lo que hace que la deduplicación funcione dentro de una misma
    # transacción: las sesiones del proyecto van con `autoflush=False`
    # (database.py), así que sin esto la fila recién agregada es invisible para
    # la consulta del `emit` siguiente y el mismo hecho entra dos veces —una sola
    # respuesta puede disparar racha y nivel juntos, y `/player` llama a
    # `on_signup` en cada arranque de sesión.
    #
    # No cubre dos transacciones simultáneas: para eso haría falta un UNIQUE. No
    # vale la pena — lo peor que pasa es una línea repetida en un feed.
    db.flush()
    return event


def recent(
    db: Session,
    after_id: int = 0,
    limit: int = FEED_LIMIT,
    before_id: int = 0,
) -> list[EventView]:
    """Los últimos eventos, del más nuevo al más viejo.

    Con `after_id` devuelve solo lo que el cliente todavía no vio, que es lo que
    hace que el sondeo cueste casi nada cuando no pasa nada.

    Con `before_id` mira para el otro lado: lo que hay MÁS VIEJO que esa línea.
    Es lo que pide el panel al llegar arriba de todo scrolleando. Los dos son
    excluyentes —son dos direcciones, no dos filtros— y el llamador elige uno.

    El tope se acota contra `MAX_LIMIT` y no contra `FEED_LIMIT`: aquel es el
    tamaño de la primera pantalla, este es lo máximo que se puede pedir de una.
    Mientras fueron el mismo número, pedir más de cuarenta era imposible aunque
    el parámetro existiera.
    """
    now = _now()
    q = db.query(GameEvent)
    if before_id:
        q = q.filter(GameEvent.id < before_id)
    elif after_id:
        q = q.filter(GameEvent.id > after_id)
    rows = q.order_by(GameEvent.id.desc()).limit(max(1, min(limit, MAX_LIMIT))).all()
    return [
        EventView(
            id=r.id,
            kind=r.kind,
            text=r.text,
            emoji=r.emoji,
            actor_alias=r.actor_alias,
            actor_level=r.actor_level,
            actor_b_alias=r.actor_b_alias,
            # En el mismo orden en que aparecen {u0} y {u1} en el texto.
            universities=[u for u in (r.university, r.university_b) if u],
            university=r.university,
            university_b=r.university_b,
            player_id=r.player_id,
            # Segundos y no un instante, por la misma razón que en boosts.py:
            # los datetime del proyecto son naive UTC y compararlos contra el
            # reloj del cliente es pedir un bug de zonas horarias.
            seconds_ago=max(0, int((now - r.created_at).total_seconds())),
        )
        for r in rows
    ]


# --- emisores ---------------------------------------------------------------

def _real(player: GamePlayer) -> bool:
    """¿Es una persona? Los sembrados no generan eventos con nombre propio."""
    return not bool(player.is_bot)


def on_signup(db: Session, player: GamePlayer) -> None:
    """Alguien dejó de ser invitado y se registró.

    Si entró por el link de alguien (`referred_by`, ver game/referrals.py), el
    anuncio es del RECLUTADOR y no del genérico: "{a} reclutó a {b}" cuenta más
    que "{b} se sumó al juego" —nombra el mérito de traerlo, no solo el hecho de
    haber llegado— así que reemplaza al signup de siempre en vez de sumarse a él.
    Sin reclutador, sigue siendo el anuncio genérico.
    """
    if not _real(player):
        return
    if player.referred_by is not None:
        referente = db.query(GamePlayer).filter(GamePlayer.id == player.referred_by).first()
        if referente is not None:
            emit(
                db,
                "referral",
                "{a} reclutó a {b}.",
                actor_alias=f"@{referente.alias}",
                actor_level=elo.level_of(referente.theta),
                actor_b_alias=f"@{player.alias}",
                # El protagonista del festejo es quien trajo, no quien llegó.
                player_id=referente.id,
                university=referente.university,
                # Una sola vez por RECLUTA, para siempre: el link guest→user es
                # idempotente y se puede volver a llamar.
                dedupe_key=f"signup:{player.id}",
            )
            return
    emit(
        db,
        "signup",
        "{a} se sumó al juego.",
        actor_alias=f"@{player.alias}",
        actor_level=elo.level_of(player.theta),
        player_id=player.id,
        university=player.university,
        # Una sola vez por jugador, para siempre: el link guest→user es
        # idempotente y se puede volver a llamar.
        dedupe_key=f"signup:{player.id}",
    )


def on_aforo(
    db: Session,
    university: str,
    personas: int,
    multiplier: float,
    horas: int,
) -> None:
    """Una universidad llegó al aforo del día y se ganó el empuje.

    Sin `actor_alias`: no lo hizo nadie en particular, lo hicieron diez. La
    frase nombra a la universidad y no a una persona, que es exactamente la
    diferencia con `on_boost` — ahí hay alguien que puso plata y merece que se
    lo vea.
    """
    art = article_for(university).capitalize()
    mult = f"×{multiplier:.1f}".replace(".", ",")
    reloj = "una hora" if horas == 1 else f"{horas} horas"
    emit(
        db,
        "boost",
        f"{art} {{u0}} llegó a {personas} personas nuevas hoy: {mult} por {reloj}. 🎉",
        university=university,
    )


def on_boost(
    db: Session,
    university: str | None,
    cafecitos: int,
    multiplier: float,
    donor_name: str | None,
) -> None:
    """Alguien invitó cafecitos. `university=None` es el empuje global."""
    quien = donor_name.strip() if donor_name and donor_name.strip() else "Alguien"
    cuantos = "un cafecito" if cafecitos == 1 else f"{cafecitos} cafecitos"
    mult = f"×{multiplier:.1f}".replace(".", ",")
    if university is None:
        # La donación que no se pudo atribuir no se pierde: la cobra todo el
        # mundo, y el feed lo cuenta como lo que es, un regalo para todos.
        emit(
            db,
            "boost",
            f"{{a}} invitó {cuantos} para TODOS: {mult} para todo el juego.",
            actor_alias=quien,
        )
        return
    art = article_for(university)
    emit(
        db,
        "boost",
        f"{{a}} invitó {cuantos} para {art} {{u0}}: {mult} para toda la universidad.",
        # Sin nivel a propósito: quien dona escribe su nombre en Cafecito y no es
        # necesariamente un jugador, así que el nombre va destacado pero sin el
        # color de un nivel que no le corresponde.
        actor_alias=quien,
        university=university,
    )


def on_answer(
    db: Session,
    player: GamePlayer,
    rank_before: int | None,
    rank_after: int | None,
    level_before: int,
    level_after: int,
) -> None:
    """Todo lo que una sola respuesta correcta puede volver noticia."""
    if not _real(player):
        return

    # Puntero nuevo. Va primero porque es el evento más fuerte y no tiene sentido
    # contar además que "pasó a N personas" en la misma respuesta.
    if rank_after == 1 and (rank_before or 0) > 1:
        emit(
            db,
            "lead",
            "{a} es el nuevo número 1.",
            actor_alias=f"@{player.alias}",
            actor_level=elo.level_of(player.theta),
            player_id=player.id,
            university=player.university,
            # Si el puntero cambia de manos de ida y de vuelta, que se cuente de
            # nuevo — pero no dos veces por la misma llegada.
            dedupe_key=f"lead:{player.id}",
            dedupe_minutes=60,
        )
    elif rank_before is not None and rank_after is not None:
        ganados = rank_before - rank_after
        if ganados >= CLIMB_MIN:
            emit(
                db,
                "climb",
                f"{{a}} pasó a {ganados} personas de una.",
                actor_alias=f"@{player.alias}",
                actor_level=elo.level_of(player.theta),
                player_id=player.id,
                university=player.university,
            )

    if player.current_combo in STREAK_MILESTONES:
        emit(
            db,
            "streak",
            f"{{a}} lleva {player.current_combo} seguidas sin errar.",
            actor_alias=f"@{player.alias}",
            actor_level=elo.level_of(player.theta),
            player_id=player.id,
            university=player.university,
            dedupe_key=f"streak:{player.id}:{player.current_combo}",
        )

    if level_after > level_before:
        emit(
            db,
            "level",
            "{a} desbloqueó derivadas más difíciles.",
            actor_alias=f"@{player.alias}",
            actor_level=level_after,
            player_id=player.id,
            university=player.university,
            dedupe_key=f"level:{player.id}:{level_after}",
        )


# --- universidades -------------------------------------------------------------

def _university_standings(db: Session, min_players: int) -> list[tuple[str, float]]:
    """(sigla, Elo promedio) de las universidades rankeadas, de mayor a menor.

    Mismo criterio que el endpoint del ranking —Elo promedio, contando solo a los
    que ya salieron de la rampa, y un mínimo de jugadores—. Si acá se ordenara
    distinto, el feed contaría sobrepasos que la tabla no muestra.
    """
    rows = (
        db.query(
            GamePlayer.university,
            func.count(GamePlayer.id),
            func.coalesce(func.sum(GamePlayer.theta), 0.0),
        )
        .filter(
            GamePlayer.university.isnot(None),
            GamePlayer.university != "",
            # El mismo filtro de actividad que la tabla, importado y no copiado:
            # si se separan, el feed cuenta sobrepasos que el ranking no muestra.
            # Vivía en `router.py`, que esto no puede importar sin cerrar un
            # ciclo, y por eso estaba reinlineado — ahora vive un nivel más abajo.
            ranking.RESOLVIO_ACA,
            GamePlayer.n_updates >= elo.RAMP_UPDATES,
        )
        .group_by(GamePlayer.university)
        .all()
    )
    standings = [
        (uni, float(theta_sum) / rated)
        for uni, rated, theta_sum in rows
        if rated >= min_players
    ]
    standings.sort(key=lambda r: r[1], reverse=True)
    return standings


def sync_universities(db: Session, min_players: int, now: datetime | None = None) -> None:
    """Compara el orden de las universidades contra la foto anterior y cuenta lo que
    cambió: quién pasó a quién, y quién viene pisándole los talones a quién.

    Se llama desde el tick de la simulación, que ya corre cada 10 s empujado por
    el tráfico — no hace falta ni worker ni cron.
    """
    now = now or _now()
    standings = _university_standings(db, min_players)
    order = [uni for uni, _ in standings]

    state = db.query(GameSimState).filter(GameSimState.id == 1).first()
    if state is None:
        return
    previous = []
    if state.uni_order_json:
        try:
            previous = json.loads(state.uni_order_json)
        except ValueError:
            previous = []
    state.uni_order_json = json.dumps(order)

    # La primera vez no hay contra qué comparar: se guarda la foto y nada más.
    # Sin esto, el primer tick after del deploy anunciaría cien sobrepasos que en
    # realidad nunca ocurrieron.
    if not previous:
        return

    rank_before = {uni: i for i, uni in enumerate(previous)}
    for i, uni in enumerate(order):
        before = rank_before.get(uni)
        if before is None or before <= i:
            continue
        # Subió: a quién pasó. Se nombra solo al que quedó justo debajo, que es
        # el sobrepaso que se ve en la tabla; listar a todos sería un párrafo.
        if i + 1 >= len(order):
            continue
        superada = order[i + 1]
        if rank_before.get(superada, -1) >= before:
            continue
        # "la UNSAM le pasó a la UNL" / "el ITBA…": el artículo lo decide el
        # nombre completo de cada casa de estudios, no la sigla.
        a0, a1 = article_for(uni).capitalize(), article_for(superada)
        emit(
            db,
            "uni_pass",
            f"{a0} {{u0}} le pasó a {a1} {{u1}} en el ranking de universidades.",
            university=uni,
            university_b=superada,
            dedupe_key=f"pass:{uni}:{superada}",
            dedupe_minutes=UNI_CLOSE_COOLDOWN_MINUTES,
            now=now,
        )

    # Las que vienen pisando. Se anuncia UNA sola por barrido, la más ajustada:
    # con el ranking parejo hay varias parejas dentro del umbral a la vez, y
    # medido en la base de desarrollo eso metía tres líneas iguales en un mismo
    # tick — el aviso deja de ser una noticia y tapa lo que sí lo es.
    disputas = []
    for i in range(len(standings) - 1):
        arriba, avg_arriba = standings[i]
        abajo, avg_abajo = standings[i + 1]
        if avg_arriba <= 0:
            continue
        margen = (avg_arriba - avg_abajo) / avg_arriba
        if margen > UNI_CLOSE_RATIO:
            continue
        disputas.append((margen, abajo, arriba))

    for margen, abajo, arriba in sorted(disputas)[:1]:
        a0, a1 = article_for(abajo).capitalize(), article_for(arriba)
        emit(
            db,
            "uni_close",
            f"{a0} {{u0}} está a nada de pasar a {a1} {{u1}}.",
            university=abajo,
            university_b=arriba,
            dedupe_key=f"close:{abajo}:{arriba}",
            dedupe_minutes=UNI_CLOSE_COOLDOWN_MINUTES,
            now=now,
        )


def prune(db: Session, now: datetime | None = None) -> int:
    """Barre lo viejo. Devuelve cuántas filas se fueron."""
    now = now or _now()
    return (
        db.query(GameEvent)
        .filter(GameEvent.created_at < now - timedelta(days=PRUNE_DAYS))
        .delete(synchronize_session=False)
    )
