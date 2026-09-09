"""Avisos push del minijuego: suscripciones, preferencias y a quién le toca hoy.

Gemelo de `push_store.py` y con la misma forma de salida, para que el notifier no
tenga que distinguir: `correrTanda` está parametrizada por ruta y estos endpoints
devuelven lo mismo que los de Intervalo.

**Por qué existe en vez de reusar aquel.** Tres cortes, y cada uno alcanza solo:

  1. `push_subscriptions.user_id` es NOT NULL y `/push/subscribe` pide sesión de
     Clerk, así que un invitado no puede suscribirse — y el invitado es entre el
     50% y el 95% de cada cohorte del juego, según la semana.
  2. `due_notifications` corta en `pending_topic_count(...) == 0`, que cuenta
     repasos SM-2 pendientes. Un usuario solo-dx tiene cero filas en
     `unit_states`, así que el aviso programado no le sale NUNCA, aunque esté
     registrado y suscripto.
  3. Todo el contexto de aquel pool sale de `answers`, `sessions`, `enrollments`
     y `users.total_xp`, y la XP del juego jamás toca ninguna de esas.

**El cupo es de la PERSONA, no del producto.** Tres avisos por día en total —uno
programado y dos reactivos—, y el juego elige primero. Sale casi gratis porque
`titular_del_cupo` devuelve el `User` cuando el jugador tiene cuenta: se reclama
contra los mismos contadores que mira Intervalo, con el mismo `claim_event_slot`,
así que un día en que dx tenga tres cosas que decir Intervalo no manda nada. El
orden lo garantiza el cron del notifier, que corre las tandas del juego antes.
Las columnas `notify_*` de `game_players` son entonces solo del invitado.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from models import (
    GameAttempt,
    GameEvent,
    GameNotificationSend,
    GamePlayer,
    GamePushSubscription,
    User,
)

# Se importan de `push_store` y no se reescriben, incluidos los dos privados: son
# "en qué huso vive esta persona" y "a qué franja de quince minutos cae esta
# hora". Tener una segunda definición de cualquiera de las dos es exactamente la
# clase de divergencia que este repo ya pagó una vez (ver el hueco de las 23:00
# en `en_horario_de_avisos`).
from push_store import (  # noqa: F401
    MIN_XP_RECLUTA_PARA_AVISAR,
    _floor_to_15,
    _zona_de,
    claim_event_slot,
    en_horario_de_avisos,
)

from . import boosts, ranking
from . import notification_copy as copy


def titular_del_cupo(player: GamePlayer):
    """Contra quién se reclama el cupo y de quién son las preferencias.

    El usuario si el jugador tiene cuenta, y el jugador mismo si es invitado. Los
    dos objetos tienen las mismas columnas `notify_*` a propósito: el resolutor
    las lee por duck typing y no le importa cuál le tocó.
    """
    return player.user if player.user_id else player


# ── Suscripciones ────────────────────────────────────────────────────────────

def upsert_subscription(
    db: DBSession, player: GamePlayer, endpoint: str, p256dh: str, auth: str
) -> GamePushSubscription:
    """Guarda (o muda) la suscripción de un navegador.

    `endpoint` es único a secas y no por jugador: es un navegador. Si alguien
    juega de invitado, se registra y vuelve a activar, la suscripción tiene que
    MUDARSE al jugador nuevo en vez de duplicarse — si no, ese aparato recibiría
    el mismo aviso dos veces.

    A diferencia de Intervalo, acá no se borran las demás filas del jugador: allá
    es un dispositivo por persona a propósito, y no hay motivo para estrenar esa
    restricción en un juego que se abre desde el teléfono y desde la compu.
    """
    fila = (
        db.query(GamePushSubscription)
        .filter(GamePushSubscription.endpoint == endpoint)
        .first()
    )
    if fila is None:
        fila = GamePushSubscription(
            player_id=player.id, endpoint=endpoint, p256dh=p256dh, auth=auth
        )
        db.add(fila)
    else:
        fila.player_id = player.id
        fila.p256dh = p256dh
        fila.auth = auth
    db.commit()
    return fila


def delete_subscription(db: DBSession, player: GamePlayer, endpoint: str) -> None:
    db.query(GamePushSubscription).filter(
        GamePushSubscription.player_id == player.id,
        GamePushSubscription.endpoint == endpoint,
    ).delete()
    db.commit()


def delete_subscriptions_by_id(db: DBSession, ids: list[int]) -> int:
    """Las que el push service dio por muertas (404/410), que reporta el notifier."""
    if not ids:
        return 0
    n = (
        db.query(GamePushSubscription)
        .filter(GamePushSubscription.id.in_(ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return n


def record_delivery_results(db: DBSession, resultados: list[tuple[int, str]]) -> int:
    """Qué contestó el push service por cada envío.

    La fila se crea al elegir el copy, o sea ANTES de intentar mandar: sin esto,
    un aviso que nunca salió queda idéntico a uno que la persona ignoró.
    """
    if not resultados:
        return 0
    ahora = datetime.utcnow()
    n = 0
    for envio_id, estado in resultados:
        fila = db.get(GameNotificationSend, envio_id)
        if fila is None or fila.delivery_status is not None:
            continue
        fila.delivery_status = (estado or "")[:20]
        fila.delivered_at = ahora
        n += 1
    db.commit()
    return n


def mark_notification_opened(db: DBSession, envio_id: int, endpoint: str) -> bool:
    """El tap, que reporta el service worker. Idempotente: gana el primero.

    Se valida que el endpoint sea de la misma persona a la que se le mandó: el
    endpoint de diagnóstico no pide sesión, así que sin esto cualquiera podría
    marcar como abierto el aviso de otro.
    """
    fila = db.get(GameNotificationSend, envio_id)
    if fila is None or fila.opened_at is not None:
        return False
    sub = (
        db.query(GamePushSubscription)
        .filter(GamePushSubscription.endpoint == endpoint)
        .first()
    )
    if sub is None or sub.player_id != fila.player_id:
        return False
    fila.opened_at = datetime.utcnow()
    db.commit()
    return True


# ── Preferencias ─────────────────────────────────────────────────────────────

def get_settings(player: GamePlayer) -> dict:
    t = titular_del_cupo(player)
    return {
        "enabled": bool(t.notify_enabled),
        "time": t.notify_time,
        "timezone": t.notify_timezone,
    }


def save_settings(
    db: DBSession,
    player: GamePlayer,
    *,
    enabled: bool,
    time: str | None,
    timezone: str | None,
) -> dict:
    t = titular_del_cupo(player)
    t.notify_enabled = enabled
    if time is not None:
        t.notify_time = time
    if timezone is not None:
        t.notify_timezone = timezone
    db.commit()
    return get_settings(player)


def copiar_preferencias_al_usuario(player: GamePlayer, user: User) -> None:
    """Al registrarse, lo que eligió de invitado se muda a su cuenta.

    Solo si el usuario no tenía nada configurado: quien ya usaba Intervalo eligió
    su horario allá, y una partida de invitado no puede pisárselo.
    """
    if user.notify_enabled or user.notify_time:
        return
    if not player.notify_enabled:
        return
    user.notify_enabled = True
    user.notify_time = player.notify_time
    user.notify_timezone = player.notify_timezone


# ── Contexto ─────────────────────────────────────────────────────────────────

def _lunes(d: date) -> date:
    return d - timedelta(days=d.weekday())


class _Cache:
    """Lo que es igual para todos los jugadores de un mismo tick.

    Sin esto, cada persona con universidad pagaba tres recorridos completos de
    `game_players` y uno de `game_attempts`. Mismo criterio que `_TickCache` en
    push_store.py.
    """

    def __init__(self, db: DBSession, ahora: datetime):
        self.db = db
        self.ahora = ahora
        self._empujes: dict | None = None
        self._companeros: dict[str, int] | None = None
        self._xp_uni: dict[str, int] | None = None
        self._xp_jugador: dict[int, int] | None = None
        self._puestos: list[tuple[int, str]] | None = None

    def empuje_de(self, universidad: str | None):
        """El empuje vigente de esa universidad, o el global si no hay propio."""
        if self._empujes is None:
            self._empujes = {v.university: v for v in boosts.active_boosts(self.db, self.ahora)}
        return self._empujes.get(universidad) or self._empujes.get(None)

    def companeros_hoy(self, universidad: str) -> int:
        if self._companeros is None:
            desde = self.ahora - timedelta(hours=24)
            filas = (
                self.db.query(GamePlayer.university, func.count(GamePlayer.id))
                .filter(
                    GamePlayer.university.isnot(None),
                    GamePlayer.is_bot.is_(False),
                    GamePlayer.last_seen_at >= desde,
                )
                .group_by(GamePlayer.university)
                .all()
            )
            self._companeros = {u: int(n) for u, n in filas}
        return self._companeros.get(universidad, 0)

    def _cargar_xp(self) -> None:
        if self._xp_uni is not None:
            return
        desde = datetime.combine(_lunes(self.ahora.date()), datetime.min.time())
        filas = (
            self.db.query(
                GamePlayer.university,
                GameAttempt.player_id,
                func.coalesce(func.sum(GameAttempt.xp_awarded), 0),
            )
            .join(GamePlayer, GamePlayer.id == GameAttempt.player_id)
            .filter(GameAttempt.created_at >= desde, GamePlayer.is_bot.is_(False))
            .group_by(GamePlayer.university, GameAttempt.player_id)
            .all()
        )
        self._xp_uni = {}
        self._xp_jugador = {}
        for uni, pid, xp in filas:
            self._xp_jugador[pid] = int(xp or 0)
            if uni:
                self._xp_uni[uni] = self._xp_uni.get(uni, 0) + int(xp or 0)

    def xp_universidad(self, universidad: str) -> int:
        self._cargar_xp()
        return (self._xp_uni or {}).get(universidad, 0)

    def xp_jugador(self, player_id: int) -> int:
        self._cargar_xp()
        return (self._xp_jugador or {}).get(player_id, 0)

    def puestos(self) -> list[tuple[int, str]]:
        """El ranking del juego, en orden, como (id, alias).

        Por XP, que es como se ordena el ranking de PERSONAS
        (`ranking.ORDEN_XP`). El de universidades va por Elo y es otra cosa —ver
        el encabezado de notification_copy.py—.
        """
        if self._puestos is None:
            self._puestos = [
                (pid, alias)
                for pid, alias in self.db.query(GamePlayer.id, GamePlayer.alias)
                .filter(ranking.RESOLVIO_ACA, GamePlayer.is_bot.is_(False))
                .order_by(*ranking.ORDEN_XP)
                .all()
            ]
        return self._puestos


def _dias_inactivo(player: GamePlayer, tz: ZoneInfo, hoy: date) -> int | None:
    if player.last_seen_at is None:
        return None
    visto = player.last_seen_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()
    return max(0, (hoy - visto).days)


def _contexto_programado(
    player: GamePlayer, cache: _Cache, tz: ZoneInfo, hoy: date
) -> dict:
    uni = player.university or None
    ctx: dict = {
        "universidad": uni,
        "dias_inactivo": _dias_inactivo(player, tz, hoy),
        "mejor_tanda": player.best_combo or 0,
    }
    if uni:
        ctx["companeros_hoy"] = cache.companeros_hoy(uni)
        ctx["xp_universidad"] = cache.xp_universidad(uni)
        ctx["xp_propia"] = cache.xp_jugador(player.id)
    return ctx


def _contexto_empuje(player: GamePlayer, cache: _Cache, jugo_hoy: bool) -> dict | None:
    vista = cache.empuje_de(player.university or None)
    if vista is None or vista.multiplier <= 1.0:
        return None
    return {
        "universidad": vista.university,
        "donante": vista.donor_name,
        "empuje_mult": vista.multiplier,
        "empuje_horas": max(1, vista.expires_in_seconds // 3600),
        "jugo_hoy": jugo_hoy,
    }


def _contexto_recluta(db: DBSession, player: GamePlayer) -> dict | None:
    """La XP NUEVA que le generaron sus reclutas, y a quién marcar después.

    Solo para INVITADOS. Al registrado ya se lo avisa Intervalo por el mismo
    hecho (`push_store._eventos_pendientes`, categoría `recruit`), que lee
    `users.referred_by_player_id`; mandarle los dos sería contarle dos veces lo
    mismo y gastarle dos de sus tres avisos del día.

    Un invitado puede reclutar —de hecho `users.referred_by_player_id` apunta a
    `game_players` justamente por eso— y hoy no recibe nada. Este es el agujero
    que cierra.
    """
    if player.user_id is not None:
        return None

    jugadores = (
        db.query(GamePlayer)
        .filter(
            GamePlayer.referred_by == player.id,
            GamePlayer.referral_xp_given > GamePlayer.referral_xp_push_seen,
        )
        .all()
    )
    usuarios = (
        db.query(User)
        .filter(
            User.referred_by_player_id == player.id,
            User.referral_xp_given > User.referral_xp_push_seen,
        )
        .all()
    )
    if not jugadores and not usuarios:
        return None

    nuevo = sum(
        (j.referral_xp_given or 0) - (j.referral_xp_push_seen or 0) for j in jugadores
    ) + sum(
        (u.referral_xp_given or 0) - (u.referral_xp_push_seen or 0) for u in usuarios
    )
    if nuevo < MIN_XP_RECLUTA_PARA_AVISAR:
        return None

    cuantos = len(jugadores) + len(usuarios)
    alias = jugadores[0].alias if jugadores else (usuarios[0].username or None)
    primero = not _hubo_aviso_de(db, player.id, copy.CAT_RECLUTA)
    return {
        "recluta_xp": nuevo,
        "reclutas": cuantos,
        "recluta_alias": alias if cuantos == 1 else None,
        "primer_recluta": primero and cuantos == 1,
        "_marcar": (jugadores, usuarios),
    }


def _contexto_ranking(player: GamePlayer, cache: _Cache) -> dict | None:
    orden = cache.puestos()
    posicion = next((i for i, (pid, _) in enumerate(orden) if pid == player.id), None)
    if posicion is None:
        return None
    puesto = posicion + 1
    anterior = player.notify_last_rank
    if anterior is None or puesto <= anterior:
        return None
    return {
        "perdio_puesto": True,
        "rival_alias": orden[posicion - 1][1] if posicion > 0 else None,
        "_puesto": puesto,
    }


def _contexto_universidad(db: DBSession, player: GamePlayer, desde: datetime) -> dict | None:
    """Lo que pasó con su universidad desde el último aviso.

    Se lee del feed del juego, que ya calcula los sobrepasos en el tick de la
    simulación (`game/events.py :: sync_universities`). Recalcularlos acá daría
    un aviso que puede contradecir lo que la persona ve en pantalla.
    """
    uni = player.university
    if not uni:
        return None
    evento = (
        db.query(GameEvent)
        .filter(
            GameEvent.kind.in_(("uni_pass", "uni_close")),
            GameEvent.created_at >= desde,
            ((GameEvent.kind == "uni_pass") & (GameEvent.university == uni))
            | ((GameEvent.kind == "uni_close") & (GameEvent.university_b == uni)),
        )
        .order_by(GameEvent.created_at.desc())
        .first()
    )
    if evento is None:
        return None
    if evento.kind == "uni_pass":
        return {"uni_paso": True, "universidad": uni, "rival_universidad": evento.university_b}
    return {"uni_cerca": True, "universidad": uni, "rival_universidad": evento.university}


def _hubo_aviso_de(db: DBSession, player_id: int, categoria: str) -> bool:
    return (
        db.query(GameNotificationSend.id)
        .filter(
            GameNotificationSend.player_id == player_id,
            GameNotificationSend.category == categoria,
        )
        .first()
        is not None
    )


def _ya_avisado_desde(
    db: DBSession, player_id: int, categoria: str, desde: datetime
) -> bool:
    return (
        db.query(GameNotificationSend.id)
        .filter(
            GameNotificationSend.player_id == player_id,
            GameNotificationSend.category == categoria,
            GameNotificationSend.sent_at >= desde,
        )
        .first()
        is not None
    )


# ── Resolución ───────────────────────────────────────────────────────────────

def _suscripciones(db: DBSession, player_id: int) -> list[GamePushSubscription]:
    return (
        db.query(GamePushSubscription)
        .filter(GamePushSubscription.player_id == player_id)
        .all()
    )


def _candidatos(db: DBSession) -> list[GamePlayer]:
    """Los jugadores con al menos un navegador suscripto.

    El filtro de `notify_enabled` va en Python y no acá porque la preferencia
    puede vivir en `users` o en `game_players` según la persona, y eso no se
    expresa en un WHERE sin un OUTER JOIN que no vale la pena: los suscriptos son
    pocos y el `IN` ya los acota.
    """
    ids = {r[0] for r in db.query(GamePushSubscription.player_id).distinct().all()}
    if not ids:
        return []
    return (
        db.query(GamePlayer)
        .filter(GamePlayer.id.in_(ids), GamePlayer.is_bot.is_(False))
        .all()
    )


def _payload(envio: GameNotificationSend, subs: list[GamePushSubscription]) -> dict:
    return {
        "player_id": envio.player_id,
        "title": envio.title,
        "body": envio.body,
        "notification_id": envio.id,
        "url": copy.URL,
        "subscriptions": [
            {"id": s.id, "endpoint": s.endpoint, "p256dh": s.p256dh, "auth": s.auth}
            for s in subs
        ],
    }


def _anotar(
    db: DBSession, player: GamePlayer, categoria: str, variante, contexto: dict
) -> GameNotificationSend:
    titulo, cuerpo = variante.render(contexto)
    envio = GameNotificationSend(
        player_id=player.id,
        category=categoria,
        variant_key=variante.key,
        title=titulo[:200],
        body=cuerpo[:500],
        sent_at=datetime.utcnow(),
    )
    db.add(envio)
    db.flush()
    return envio


def due_game_notifications(db: DBSession, force: bool = False) -> list[dict]:
    """El aviso programado: sale porque llegó el horario que la persona eligió.

    Uno por día como máximo, reclamado en la misma transacción que lo decide
    (claim-on-read), igual que el de Intervalo y por el mismo motivo: sin eso, un
    tick reintentado manda dos veces.
    """
    ahora = datetime.now(tz=ZoneInfo("UTC"))
    cache = _Cache(db, ahora.replace(tzinfo=None))
    salida: list[dict] = []

    for player in _candidatos(db):
        titular = titular_del_cupo(player)
        if titular is None or not titular.notify_enabled or not titular.notify_time:
            continue
        tz = _zona_de(titular)
        local = ahora.astimezone(tz)
        hoy = local.date()

        if not force:
            franja = f"{local.hour:02d}:{_floor_to_15(local.minute):02d}"
            if franja != titular.notify_time:
                continue
            if titular.notify_last_sent_on == hoy:
                continue

        dias = _dias_inactivo(player, tz, hoy)
        # Un mes de silencio con los avisos prendidos no es alguien a quien le
        # falte un recordatorio: se apaga el canal y listo.
        if dias is not None and dias >= copy.DIAS_PARA_APAGAR:
            titular.notify_enabled = False
            continue

        subs = _suscripciones(db, player.id)
        if not subs:
            continue

        contexto = _contexto_programado(player, cache, tz, hoy)
        elegido = copy.elegir_programada(
            contexto=contexto,
            ultima_categoria=titular.notify_last_category,
            ultima_variante=titular.notify_last_variant_key,
        )
        if elegido is None:
            continue
        categoria, variante = elegido

        envio = _anotar(db, player, categoria, variante, contexto)
        titular.notify_last_sent_on = hoy
        titular.notify_last_category = categoria
        titular.notify_last_variant_key = variante.key
        # La foto del puesto se refresca acá aunque el aviso no sea de ranking:
        # es "el último puesto que sabíamos de esta persona", y sin actualizarlo
        # el aviso reactivo de «te pasaron» compararía para siempre contra el
        # primer valor que se haya guardado.
        orden = cache.puestos()
        pos = next((i for i, (pid, _) in enumerate(orden) if pid == player.id), None)
        if pos is not None:
            player.notify_last_rank = pos + 1
        salida.append(_payload(envio, subs))

    db.commit()
    return salida


def due_game_event_notifications(db: DBSession, force: bool = False) -> list[dict]:
    """Los avisos reactivos: salen porque pasó algo.

    Hasta dos por día, con el cupo del `titular_del_cupo` — o sea que un jugador
    registrado le come el cupo a Intervalo, que es el orden buscado.

    La ventana horaria es la misma que eligió para el programado: estos se
    disparan cuando alguien dona o cuando un recluta juega, y eso puede pasar a
    las cuatro de la mañana.
    """
    ahora = datetime.now(tz=ZoneInfo("UTC"))
    ahora_naive = ahora.replace(tzinfo=None)
    cache = _Cache(db, ahora_naive)
    salida: list[dict] = []

    for player in _candidatos(db):
        titular = titular_del_cupo(player)
        if titular is None or not titular.notify_enabled:
            continue
        if not force and not en_horario_de_avisos(titular, ahora):
            continue

        tz = _zona_de(titular)
        hoy = ahora.astimezone(tz).date()
        medianoche = datetime.combine(hoy, datetime.min.time())
        jugo_hoy = (
            player.last_seen_at is not None
            and _dias_inactivo(player, tz, hoy) == 0
        )

        subs = _suscripciones(db, player.id)
        if not subs:
            continue

        for categoria in copy.ORDEN_REACTIVAS:
            if _ya_avisado_desde(db, player.id, categoria, medianoche):
                continue
            if categoria == copy.CAT_EMPUJE:
                contexto = _contexto_empuje(player, cache, jugo_hoy)
            elif categoria == copy.CAT_RECLUTA:
                contexto = _contexto_recluta(db, player)
            elif categoria == copy.CAT_RANKING:
                contexto = _contexto_ranking(player, cache)
            else:
                contexto = _contexto_universidad(db, player, medianoche)
            if contexto is None:
                continue
            variante = copy.elegir_reactiva(categoria, contexto)
            if variante is None:
                continue
            if not force and not claim_event_slot(db, titular, hoy):
                break

            envio = _anotar(db, player, categoria, variante, contexto)
            # Lo que hay que marcar para no volver a contar lo mismo mañana. Va
            # en la MISMA transacción que el envío: si se commiteara después, un
            # corte en el medio deja el aviso mandado y la marca sin poner.
            marcar = contexto.get("_marcar")
            if marcar:
                for fila in (*marcar[0], *marcar[1]):
                    fila.referral_xp_push_seen = fila.referral_xp_given
            if contexto.get("_puesto"):
                player.notify_last_rank = contexto["_puesto"]
            salida.append(_payload(envio, subs))
            break

    db.commit()
    return salida
