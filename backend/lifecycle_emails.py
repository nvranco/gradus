"""
lifecycle_emails.py — Emails automáticos de retención (Resend).

Disparadores, resueltos íntegramente en el backend (a diferencia del push,
el envío no necesita un worker separado — Resend se llama directo desde acá):

- "bounce": el usuario se registró pero nunca terminó una sesión. Se manda una
  sola vez (`bounce_email_sent_at`).
- "winback": el usuario terminó al menos una sesión pero no volvió en 5+ días.
  Se manda una vez por racha de inactividad — se re-arma solo si vuelve a
  terminar una sesión y cae inactivo de nuevo (`winback_email_sent_at` se
  compara contra el último `finished_at`, no solo contra "ya se mandó alguna
  vez").
- "streak tier": el usuario alcanzó un hito del multiplicador de XP (3/9/18/
  30/45 días de racha). Se felicita A LA MAÑANA SIGUIENTE (ventana 8-12 hora
  local): el multiplicador se disfruta en la próxima sesión, así el mail
  felicita y a la vez ofrece algo para hacer ahora. Si a esa altura el usuario
  ya repasó hoy por su cuenta, el hito se marca como avisado SIN mandar nada —
  volvió solo, el mail no tiene trabajo que hacer.
- "report thanks": el usuario reportó un problema en un ejercicio (botón de
  bandera, question_type="C"). Se agradece A LA MAÑANA SIGUIENTE (misma
  ventana 8-12), nunca el mismo día — un mail casi instantáneo se leería a
  respuesta automática, justo lo que el copy evita prometer. No dice que ya
  esté arreglado: solo que alguien lo va a revisar. Idempotencia por REPORTE
  (`exercise_feedback.thanks_sent_at`), no por usuario, porque un usuario
  puede reportar más de una vez; si tiene varios pendientes el día que le
  toca, se agrupan en un solo mail.

Un worker externo (notifier/) pollea `/internal/emails/run` por hora; ver
`due_bounce_emails` / `due_winback_emails` + `send_bounce_email` /
`send_winback_email`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_
from sqlalchemy.orm import Session as DBSession

# `algorithm` vive en la raíz del repo; mismo patrón que session_store para
# que el módulo también se pueda importar suelto (scripts, previews).
sys.path.insert(0, str(Path(__file__).parent.parent))

from algorithm import STREAK_TIERS
from models import ExerciseFeedback, GamePlayer, Session as SessionModel, User

BOUNCE_MIN_ACCOUNT_AGE = timedelta(hours=24)
WINBACK_INACTIVITY = timedelta(days=5)

# Ventana local del mail de hito: entre las 8 y las 12. El worker corre cada
# hora en el minuto :00, así que en la práctica llega entre las 8 y las 9.
STREAK_EMAIL_HOUR_FROM = 8
STREAK_EMAIL_HOUR_TO = 12

# Mismo criterio y mismos valores que el de arriba: al día siguiente del
# reporte, nunca el mismo día (se leería a auto-respuesta). Constante propia
# (no se reusa STREAK_EMAIL_HOUR_*) porque cada mail es dueño de su ventana,
# aunque hoy coincidan.
REPORT_THANKS_HOUR_FROM = 8
REPORT_THANKS_HOUR_TO = 12

# Misma política que session_store: la tz la reporta el navegador y puede venir
# rota; fallback Argentina, donde vive casi toda la base.
_DEFAULT_TZ = "America/Argentina/Buenos_Aires"


def _user_zone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or _DEFAULT_TZ)
    except ZoneInfoNotFoundError:
        return ZoneInfo(_DEFAULT_TZ)


def _reached_tier(streak_days: int) -> int:
    """El mayor umbral de STREAK_TIERS alcanzado por `streak_days`, 0 si
    ninguno. Derivar el hito de los días (en vez de usar `tier_reached`, que
    solo es True el día exacto) hace que el mail sobreviva a cualquier retraso
    del worker."""
    reached = 0
    for threshold, _mult in STREAK_TIERS:
        if threshold > 0 and streak_days >= threshold:
            reached = threshold
    return reached


def _tier_multiplier(threshold: int) -> float:
    return dict(STREAK_TIERS)[threshold]


# El emoji del asunto escala con el hito: el fueguito del arranque, el moai de
# quien ya se planta serio, el cerebro entrenado, el brazo bionico (el mismo
# de las push) y el GOAT en la cima.
STREAK_TIER_EMOJI = {
    3: "🔥",
    9: "🗿",
    18: "🧠",
    30: "🦾",
    45: "🐐",
}


def _next_tier(threshold: int) -> tuple[int, float] | None:
    """(umbral, multiplicador) del escalón siguiente, None en el máximo."""
    thresholds = [t for t, _ in STREAK_TIERS if t > 0]
    i = thresholds.index(threshold)
    if i + 1 >= len(thresholds):
        return None
    nxt = thresholds[i + 1]
    return nxt, _tier_multiplier(nxt)


# ── Apodo ────────────────────────────────────────────────────────────────────

def greeting_name(user: User) -> str:
    """`display_name` si parece un apodo real; si no, el primer nombre."""
    dn = (user.display_name or "").strip()
    if len(dn) >= 2 and not dn.isdigit() and "@" not in dn:
        return dn
    return (user.name or "").strip().split(" ")[0] or "che"


# ── Selección de destinatarios ───────────────────────────────────────────────

def due_bounce_emails(db: DBSession) -> list[User]:
    """Usuarios registrados hace 24h+ que nunca terminaron una sesión NI jugaron.

    El «ni jugaron» es lo que se agregó, y arregla un mail que estaba mintiendo:
    la condición era solo «ninguna sesión de Intervalo terminada», así que TODO
    usuario que venga del minijuego la cumple para siempre y recibía «Tu cuenta
    ya está lista… Solo falta tu primera sesión». Esa persona jugó, y a veces
    cien derivadas.

    No era visible mientras el juego casi no registraba a nadie; con el pedido de
    registro funcionando pasa a ser la mayoría de las altas nuevas.
    """
    cutoff = datetime.utcnow() - BOUNCE_MIN_ACCOUNT_AGE
    finished_user_ids = (
        db.query(SessionModel.user_id)
        .filter(SessionModel.finished_at.isnot(None))
        .distinct()
    )
    jugaron = (
        db.query(GamePlayer.user_id)
        .filter(GamePlayer.user_id.isnot(None), GamePlayer.exercises_correct > 0)
        .distinct()
    )
    return (
        db.query(User)
        .filter(
            User.email_unsubscribed.is_(False),
            User.bounce_email_sent_at.is_(None),
            User.created_at <= cutoff,
            User.id.notin_(finished_user_ids),
            User.id.notin_(jugaron),
        )
        .all()
    )


# Cuánto silencio en el juego antes de mandar el "volvé".
#
# Cinco días, el mismo que el de Intervalo. No es pereza: la mediana de la
# primera sesión son 3-5 derivadas y casi nadie vuelve por su cuenta, así que la
# vara no es "cuándo se enfrió" sino "cuándo dejó de ser una molestia escribirle".
WINBACK_DX_INACTIVITY = timedelta(days=5)


def due_winback_dx_emails(db: DBSession) -> list[tuple[User, GamePlayer]]:
    """Jugadores CON CUENTA que derivaron alguna vez y hace 5 días que no.

    El invitado no entra y no puede entrar: `users.email` es NOT NULL y viene de
    Clerk, así que a quien juega sin registrarse solo se lo puede alcanzar por
    push (game/notifications.py).

    El marcador es de `game_players` y no de `users`: son dos productos, y quien
    dejó de derivar puede seguir estudiando en Intervalo —donde no hay nada que
    recuperar—. Con un solo marcador, mandar uno apagaba el otro.
    """
    cutoff = datetime.utcnow() - WINBACK_DX_INACTIVITY
    filas = (
        db.query(User, GamePlayer)
        .join(GamePlayer, GamePlayer.user_id == User.id)
        .filter(
            User.email_unsubscribed.is_(False),
            GamePlayer.is_bot.is_(False),
            GamePlayer.exercises_correct > 0,
            GamePlayer.last_seen_at.isnot(None),
            GamePlayer.last_seen_at <= cutoff,
        )
        .all()
    )
    return [
        (user, jugador)
        for user, jugador in filas
        if jugador.winback_email_sent_at is None
        or jugador.winback_email_sent_at < jugador.last_seen_at
    ]


def due_winback_emails(db: DBSession) -> list[tuple[User, datetime]]:
    """Usuarios con >=1 sesión terminada, inactivos 5+ días, sin mail ya
    mandado para esta racha de inactividad en particular. Devuelve pares
    (user, last_finished_at) — el caller necesita last_finished_at para
    setear el marcador de idempotencia."""
    cutoff = datetime.utcnow() - WINBACK_INACTIVITY
    last_finished = (
        db.query(
            SessionModel.user_id.label("user_id"),
            func.max(SessionModel.finished_at).label("last_finished_at"),
        )
        .filter(SessionModel.finished_at.isnot(None))
        .group_by(SessionModel.user_id)
        .subquery()
    )
    rows = (
        db.query(User, last_finished.c.last_finished_at)
        .join(last_finished, last_finished.c.user_id == User.id)
        .filter(
            User.email_unsubscribed.is_(False),
            last_finished.c.last_finished_at <= cutoff,
        )
        .all()
    )
    return [
        (user, last_finished_at)
        for user, last_finished_at in rows
        if user.winback_email_sent_at is None
        or user.winback_email_sent_at < last_finished_at
    ]


def due_streak_tier_emails(db: DBSession) -> tuple[list[tuple[User, int]], list[tuple[User, int]]]:
    """Candidatos del mail de hito de multiplicador.

    Devuelve dos listas de pares (user, tier): `to_send` (mandar ahora) y
    `to_mark` (marcar como avisado sin mandar: el hito fue un día anterior y
    el usuario ya volvió hoy por su cuenta — la felicitación no tiene trabajo
    que hacer).

    El filtro SQL es grueso a propósito (tier exacto y hora local se resuelven
    en Python, son un puñado de filas): streak_days en zona de hitos y marcador
    por detrás de los días — como el tier nunca supera los días, si el marcador
    ya está en streak_days o más, no puede haber hito pendiente.
    """
    candidates = (
        db.query(User)
        .filter(
            User.email_unsubscribed.is_(False),
            User.streak_days >= 3,
            or_(
                User.streak_email_sent_tier.is_(None),
                User.streak_email_sent_tier < User.streak_days,
            ),
        )
        .all()
    )

    to_send: list[tuple[User, int]] = []
    to_mark: list[tuple[User, int]] = []
    for user in candidates:
        tier = _reached_tier(user.streak_days)
        if tier <= (user.streak_email_sent_tier or 0):
            continue
        # Sin fecha de racha no hay forma de saber si "hoy ya repasó"; no
        # debería pasar con streak_days > 0, pero ante datos raros, silencio.
        if user.streak_last_date is None:
            continue
        local_now = datetime.now(_user_zone(user))
        if user.streak_last_date >= local_now.date():
            # Repaso hoy. Si streak_days == tier el hito es de HOY y el mail va
            # recien manana: ni mandar ni marcar todavia. Si es mayor, el hito
            # fue un dia anterior y hoy volvio solo: marcar sin mandar.
            if user.streak_days > tier:
                to_mark.append((user, tier))
            continue
        # Recién a la mañana siguiente, en la ventana.
        if not (STREAK_EMAIL_HOUR_FROM <= local_now.hour < STREAK_EMAIL_HOUR_TO):
            continue
        to_send.append((user, tier))
    return to_send, to_mark


def due_report_thanks_emails(db: DBSession) -> list[tuple[User, list[int]]]:
    """Reportes de contenido (question_type="C", ver main.py acción "report")
    con agradecimiento pendiente, agrupados por usuario.

    Mismo criterio de ventana que due_streak_tier_emails, reusado a propósito
    (ver REPORT_THANKS_HOUR_FROM/TO): recién al día siguiente del reporte
    (fecha local), entre las 8 y las 12. El mismo día se leería a
    auto-respuesta, justo lo que el copy evita prometer.

    Si un usuario reportó más de una vez antes de que le toque el mail, todos
    esos reportes se agrupan en UN solo envío — nunca dos agradecimientos la
    misma mañana por la misma persona. `answered_at` es NOT NULL para
    question_type="C" (main.py lo setea siempre al crear la fila)."""
    rows = (
        db.query(ExerciseFeedback, User)
        .join(User, User.id == ExerciseFeedback.user_id)
        .filter(
            ExerciseFeedback.question_type == "C",
            ExerciseFeedback.thanks_sent_at.is_(None),
            User.email_unsubscribed.is_(False),
        )
        .all()
    )

    by_user: dict[int, tuple[User, list[int]]] = {}
    for feedback, user in rows:
        tz = _user_zone(user)
        local_now = datetime.now(tz)
        reported_local_date = feedback.answered_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()
        if reported_local_date >= local_now.date():
            continue  # todavía no pasó ni un día local completo
        if not (REPORT_THANKS_HOUR_FROM <= local_now.hour < REPORT_THANKS_HOUR_TO):
            continue
        entry = by_user.setdefault(user.id, (user, []))
        entry[1].append(feedback.id)
    return list(by_user.values())


# ── Desuscripción (token sin login) ──────────────────────────────────────────

def _unsub_secret() -> str:
    secret = os.environ.get("EMAIL_UNSUB_SECRET")
    if not secret:
        raise RuntimeError("EMAIL_UNSUB_SECRET not configured")
    return secret


def unsubscribe_token(user_id: int) -> str:
    mac = hmac.new(_unsub_secret().encode(), str(user_id).encode(), hashlib.sha256)
    return f"{user_id}.{mac.hexdigest()}"


def verify_unsubscribe_token(token: str) -> int | None:
    try:
        user_id_str, mac_hex = token.split(".", 1)
        user_id = int(user_id_str)
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(_unsub_secret().encode(), user_id_str.encode(), hashlib.sha256)
    if not hmac.compare_digest(mac_hex, expected.hexdigest()):
        return None
    return user_id


# ── Plantilla HTML ────────────────────────────────────────────────────────────

# Diseño "a lo Brilliant": cuerpo claro, sin card con fondo fijo. La app de
# Gmail (iOS/Android) en modo oscuro ignora meta color-scheme y
# [data-ogsc]/[data-ogsb] y fuerza su propia inversión de colores — una card
# oscura fija (#131324) se convertía en lavanda claro y quedaba rota. Diseñando
# claro (texto oscuro sobre fondo transparente) esa inversión juega a favor: en
# modo oscuro Gmail lo pasa a texto claro sobre fondo oscuro y sigue viéndose
# intencional.

# El logo va como imagen inline (CID) en vez de HTML: Gmail no invierte las
# imágenes, así que el wordmark con su fondo #131324 y bordes redondeados se ve
# idéntico en claro y en oscuro. Además la barra de cinturones queda calzada al
# ancho exacto de la palabra, que con tablas HTML había que hardcodear (y
# quedaba corta). Se genera con scripts/gen_email_logo.py.
LOGO_PATH = Path(__file__).resolve().parent / "assets" / "email-logo.png"
LOGO_CID = "intervalo-logo"
LOGO_W, LOGO_H = 163, 61  # tamaño CSS; el PNG está a 3x para retina


def _logo_html() -> str:
    return (
        f'<img src="cid:{LOGO_CID}" width="{LOGO_W}" height="{LOGO_H}" alt="intervalo" '
        f'style="display:block;margin:0 auto 32px;border:0;outline:none;text-decoration:none;">'
    )


def _rows_html(rows: list[tuple[str, str, str]] | None, sans: str) -> str:
    """Una tabla chica de tres columnas, para los mails que muestran un listado.

    Tabla y no flex: los clientes de correo no soportan flexbox de forma
    confiable, y una lista que se desarma es peor que no tenerla. Mismo diseño
    claro que el resto de la plantilla, para que la inversión de Gmail en modo
    oscuro siga jugando a favor.

    Sin filas devuelve el string vacío, así el mail queda exactamente como antes
    y ningún llamador tiene que ramificar.
    """
    if not rows:
        return ""
    celdas = ""
    for izq, medio, der in rows:
        celdas += (
            '<tr>'
            f'<td style="{sans}font-size:14px;padding:6px 0;color:#131324;">{izq}</td>'
            f'<td style="{sans}font-size:12px;padding:6px 8px;color:#768899;text-align:center;">{medio}</td>'
            f'<td style="{sans}font-size:14px;padding:6px 0;color:#131324;font-weight:700;text-align:right;">{der}</td>'
            "</tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 24px;">' + celdas + "</table>"
    )


def render_email(*, greeting: str, highlight: str, cta_label: str, cta_url: str, unsubscribe_url: str, preview: str | None = None, rows: list[tuple[str, str, str]] | None = None) -> str:
    # La app usa DM Sans para el cuerpo; Gmail no carga webfonts, así que se
    # aproxima con un stack sans-serif web-safe (antes no se declaraba nada y
    # el cuerpo caía en Times).
    sans = "font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;"
    # Misma forma que el CTA de la app y de la landing: esquinas de 4px,
    # mayúsculas y tracking de 0.1em (1.3px sobre 13px). Antes era una píldora
    # de 8px en minúsculas, que no se parecía a ningún botón del producto. El
    # texto va en caja normal y las mayúsculas las pone el CSS: si un cliente
    # ignora text-transform, la etiqueta se sigue leyendo bien.
    btn = (
        f"display:inline-block;background:#5457e5;color:#ffffff;{sans}font-size:13px;"
        "font-weight:600;letter-spacing:1.3px;text-transform:uppercase;"
        "padding:15px 30px;border-radius:4px;text-decoration:none"
    )
    # Preheader: lo que Gmail y Apple Mail muestran como preview en la bandeja
    # y en la notificación. Sin esto el snippet se arma con TODO el texto del
    # mail en orden — botón, URL y pie incluidos. Va invisible al principio del
    # body, y el relleno de &nbsp;&zwnj; empuja lo que sigue fuera del recorte.
    # `preview` permite recortarlo (ej: el mail de hito deja la negrita solo
    # adentro del mail); por defecto es saludo + negrita.
    preview = preview if preview is not None else f"{greeting} {highlight}"
    preheader = (
        '<div style="display:none;font-size:1px;line-height:1px;max-height:0;'
        f'max-width:0;opacity:0;overflow:hidden;">{preview}{"&nbsp;&zwnj;" * 40}</div>'
    )
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<style>
  body {{ margin:0; padding:0; }}
</style>
</head>
<body>
{preheader}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:48px 16px;">
<table role="presentation" width="420" cellpadding="0" cellspacing="0" style="max-width:420px;">
<tr><td align="center" style="padding:0 24px;">
{_logo_html()}
<p style="{sans}font-size:15px;line-height:1.6;margin:0 0 8px;max-width:22rem;color:#131324;">{greeting}</p>
<p style="{sans}font-size:15px;line-height:1.6;margin:0 0 24px;font-weight:700;color:#131324;">{highlight}</p>
{_rows_html(rows, sans)}
<a href="{cta_url}" style="{btn}">{cta_label}</a>
<p style="{sans}font-size:11px;line-height:1.7;color:#768899;margin:32px 0 0">Intervalo 2026. Desarrollado por y para estudiantes.<br><a href="{_app_base_url()}/terminos" style="color:#768899">Términos y condiciones</a> &middot; <a href="{_app_base_url()}/privacidad" style="color:#768899">Política de privacidad</a> &middot; <a href="{unsubscribe_url}" style="color:#768899">Desuscribirse</a></p>
</td></tr>
</table>
</td></tr></table>
</body>
</html>"""


# ── Envío ─────────────────────────────────────────────────────────────────────

def _app_base_url() -> str:
    return os.environ.get("APP_BASE_URL", "https://www.intervalo.xyz")


def _cta_url(campaign: str, path: str = "/") -> str:
    """URL del botón, etiquetada con el tipo de mail que la generó.

    Sin esto los cuatro mails apuntan al mismo link pelado y un click es
    indistinguible de una visita cualquiera: no hay forma de saber cuál copy
    trae gente de vuelta. PostHog levanta los `utm_*` solo, así que alcanza con
    ponerlos.

    No contamina la atribución de origen: `first_utm_source` se registra con
    `register_once` (ver web/src/lib/analytics/attribution.ts) y estos mails van
    únicamente a usuarios que ya existen, o sea que ya lo tienen fijado.
    """
    return f"{_app_base_url()}{path}?utm_source=email&utm_campaign={campaign}"


def _api_base_url() -> str:
    return os.environ.get("API_BASE_URL", "https://api.intervalo.xyz")


def _send(to_email: str, subject: str, html: str, unsubscribe_url: str, text: str | None = None) -> bool:
    """Best-effort send via Resend. Returns True on success, logs and swallows
    any failure so one bad address never blocks the rest of the batch."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logging.warning("RESEND_API_KEY not set — skipping lifecycle email")
        return False

    from_email = os.environ.get("LIFECYCLE_FROM_EMAIL", "Intervalo <hola@comms.intervalo.xyz>")

    try:
        import resend

        resend.api_key = api_key
        payload = {
            "from": from_email,
            "to": to_email,
            "subject": subject,
            "html": html,
            # Sin esto Resend autogenera el texto plano convirtiendo el HTML
            # entero (botón, URL y pie incluidos), y es lo que Gmail muestra en
            # la notificación. La versión propia lleva solo el copy.
            **({"text": text} if text else {}),
            # Gmail y Yahoo ponen su propio botón de "Cancelar suscripción" arriba
            # de todo cuando existe este par de headers, y cuentan su ausencia como
            # señal negativa de reputación aunque el link esté en el pie. El POST
            # de un click lo maneja el mismo endpoint (ver main.py) — sin eso,
            # Gmail recibiría un 405 y descartaría el header.
            "headers": {
                "List-Unsubscribe": f"<{unsubscribe_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                # Los mails de ciclo de vida comparten asunto ("¡Volvé X!"):
                # sin esto Gmail los encadena en un hilo y recorta el contenido
                # repetido entre mensajes — el botón y el pie, idénticos de un
                # mail al otro, quedan escondidos detrás de los "···". Un id
                # único por envío le dice a Gmail que no son la misma
                # conversación.
                "X-Entity-Ref-ID": uuid.uuid4().hex,
            },
        }
        if LOGO_PATH.exists():
            payload["attachments"] = [
                {
                    "content": base64.b64encode(LOGO_PATH.read_bytes()).decode(),
                    "filename": "intervalo.png",
                    "content_type": "image/png",
                    "content_id": LOGO_CID,
                }
            ]
        resend.Emails.send(payload)
        return True
    except Exception:
        logging.exception("Failed to send lifecycle email via Resend to %s", to_email)
        return False


def send_bounce_email(db: DBSession, user: User) -> bool:
    name = greeting_name(user)
    unsubscribe_url = f"{_api_base_url()}/email/unsubscribe?token={unsubscribe_token(user.id)}"
    greeting = "Tu cuenta ya está lista y los ejercicios te esperan."
    highlight = "Solo falta tu primera sesión."
    html = render_email(
        greeting=greeting,
        highlight=highlight,
        cta_label="Volver",
        cta_url=_cta_url("bounce"),
        unsubscribe_url=unsubscribe_url,
    )
    sent = _send(user.email, f"¡Todo listo {name}! 🏁", html, unsubscribe_url, text=f"{greeting} {highlight}")
    if sent:
        user.bounce_email_sent_at = datetime.utcnow()
        db.commit()
    return sent


def send_winback_email(db: DBSession, user: User) -> bool:
    name = greeting_name(user)
    unsubscribe_url = f"{_api_base_url()}/email/unsubscribe?token={unsubscribe_token(user.id)}"
    greeting = "Tus temas te extrañan y te están sacando puestos en el ranking."
    highlight = "Recuperalos hoy mismo."
    html = render_email(
        greeting=greeting,
        highlight=highlight,
        cta_label="Volver",
        cta_url=_cta_url("winback"),
        unsubscribe_url=unsubscribe_url,
    )
    sent = _send(user.email, f"¡Volvé {name}! 👀", html, unsubscribe_url, text=f"{greeting} {highlight}")
    if sent:
        user.winback_email_sent_at = datetime.utcnow()
        db.commit()
    return sent


def send_winback_dx_email(db: DBSession, user: User, jugador: GamePlayer) -> bool:
    """El "volvé" del minijuego: el mismo mail que el de Intervalo, con el link
    apuntando al juego.

    Mismo asunto, mismo saludo, mismo botón. La única palabra que cambia es
    "temas" por "derivadas", porque es lo que esa persona dejó atrás.

    Que sean iguales es la decisión, y va contra lo primero que se escribió acá
    —un asunto propio, «¡Volvé a derivar X!»—: en la bandeja de entrada el
    remitente es el mismo y la marca es una sola, así que dos asuntos distintos
    para el mismo mensaje se leen como dos productos peleándose la atención de la
    misma persona. Lo que tiene que llevar a cada lado es el botón, no el
    asunto."""
    name = greeting_name(user)
    unsubscribe_url = f"{_api_base_url()}/email/unsubscribe?token={unsubscribe_token(user.id)}"
    greeting = "Tus derivadas te extrañan y te están sacando puestos en el ranking."
    highlight = "Recuperalos hoy mismo."
    html = render_email(
        greeting=greeting,
        highlight=highlight,
        cta_label="Volver",
        cta_url=_cta_url("winback_dx", path="/derivadas"),
        unsubscribe_url=unsubscribe_url,
    )
    sent = _send(
        user.email, f"¡Volvé {name}! 👀", html, unsubscribe_url,
        text=f"{greeting} {highlight}",
    )
    if sent:
        jugador.winback_email_sent_at = datetime.utcnow()
        db.commit()
    return sent


def send_cafecito_efecto_email(
    db: DBSession,
    user: User,
    *,
    university: str | None,
    xp_extra: int,
    estudiantes: int,
) -> bool:
    """Le cuenta a quien invitó un cafecito qué hizo su empuje, ya vencido.

    Se manda al VENCER y no al acreditar: recién ahí el número está cerrado. Y
    solo si hay número — con `xp_extra` en cero no se manda nada, porque un mail
    que dice "tu cafecito generó 0 XP" es peor que ningún mail.

    Ojo con lo que este mail NO puede prometer: `boosts.multiplier_for` colapsa
    el empuje global y el dirigido en un solo número, y los cafecitos de la
    ventana se suman a propósito. Así que lo honesto es "el empuje de tu
    universidad generó N XP", nunca "TU cafecito generó N XP".
    """
    if xp_extra <= 0:
        return False

    name = greeting_name(user)
    unsubscribe_url = f"{_api_base_url()}/email/unsubscribe?token={unsubscribe_token(user.id)}"
    donde = f"la {university}" if university else "todo Intervalo"
    greeting = f"{name}, tu cafecito ya terminó de hacer efecto."
    highlight = (
        f"Durante 24 horas {donde} sumó {xp_extra} XP extra, "
        f"repartidos entre {estudiantes} estudiantes."
    )
    html = render_email(
        greeting=greeting,
        highlight=highlight,
        cta_label="Ver",
        cta_url=_cta_url("cafecito_efecto"),
        unsubscribe_url=unsubscribe_url,
        preview=greeting,
    )
    asunto = f"Tu cafecito le dio {xp_extra} XP a {donde} ☕"
    return _send(user.email, asunto, html, unsubscribe_url, text=f"{greeting} {highlight}")


def send_reclutas_semanal_email(
    db: DBSession,
    user: User,
    *,
    xp_semana: int,
    filas: list[tuple[str, str, int]],
) -> bool:
    """El resumen semanal de lo que generaron los reclutas de esta persona.

    `filas` es (@alias, universidad, xp) ya ordenada por aporte, igual que la
    vista de Reclutas del ranking.

    Semana sin movimiento: NO se manda. Este mail existe para traer buenas
    noticias; mandarlo vacío lo convierte en ruido y enseña a ignorarlo, que es
    justo lo que no se quiere de un canal que se usa una vez por semana.
    """
    if xp_semana <= 0 or not filas:
        return False

    name = greeting_name(user)
    unsubscribe_url = f"{_api_base_url()}/email/unsubscribe?token={unsubscribe_token(user.id)}"
    greeting = f"{name}, esto es lo que generaron tus reclutas esta semana."
    # Singular y plural: con un solo recluta el mail decía "de 1 personas", que
    # es la clase de detalle que hace dudar de si el número está bien.
    quienes = "1 persona" if len(filas) == 1 else f"{len(filas)} personas"
    highlight = f"{xp_semana} XP esta semana, de {quienes}."
    html = render_email(
        greeting=greeting,
        highlight=highlight,
        cta_label="Ver",
        cta_url=_cta_url("reclutas_semanal"),
        unsubscribe_url=unsubscribe_url,
        preview=greeting,
        rows=[(f"@{alias}", uni or "", f"{xp} XP") for alias, uni, xp in filas],
    )
    asunto = f"Tus reclutas generaron {xp_semana} XP esta semana 🪖"
    return _send(user.email, asunto, html, unsubscribe_url, text=f"{greeting} {highlight}")


def send_streak_tier_email(db: DBSession, user: User, tier: int) -> bool:
    name = greeting_name(user)
    mult = _tier_multiplier(tier)
    unsubscribe_url = f"{_api_base_url()}/email/unsubscribe?token={unsubscribe_token(user.id)}"

    if mult == 2.0:
        gain = "vale el doble de XP"
    else:
        gain = f"suma un {round((mult - 1) * 100)}% más de XP"
    nxt = _next_tier(tier)
    if nxt is None:
        highlight = "Es el multiplicador más alto que hay. Ahora se trata de no perderlo."
    else:
        nxt_days, nxt_mult = nxt
        highlight = f"El próximo escalón es ×{nxt_mult:.1f}, a los {nxt_days} días."

    greeting = f"Llegaste a {tier} días seguidos repasando, cada ejercicio que resolvés ahora {gain} para el ranking."
    html = render_email(
        greeting=greeting,
        highlight=highlight,
        # "Continuar" y no "Volver": este mail no le habla a alguien que se fue,
        # sino a alguien que viene bien y tiene que seguir.
        cta_label="Continuar",
        cta_url=_cta_url("streak"),
        unsubscribe_url=unsubscribe_url,
        # La preview corta en el saludo: el próximo escalón (la negrita) se
        # descubre recién adentro del mail.
        preview=greeting,
    )
    emoji = STREAK_TIER_EMOJI.get(tier, "🔥")
    sent = _send(user.email, f"¡Llegaste a ×{mult:.1f} {name}! {emoji}", html, unsubscribe_url, text=greeting)
    if sent:
        user.streak_email_sent_tier = tier
        user.streak_email_sent_at = datetime.utcnow()
        db.commit()
    return sent


def send_report_thanks_email(db: DBSession, user: User, feedback_ids: list[int]) -> bool:
    """Agradece haber reportado un problema en un ejercicio. No promete que ya
    esté arreglado (no lo sabemos en el momento de mandar el mail) — solo
    confirma que alguien lo va a mirar."""
    name = greeting_name(user)
    unsubscribe_url = f"{_api_base_url()}/email/unsubscribe?token={unsubscribe_token(user.id)}"
    greeting = "Nos avisaste de un problema en un ejercicio."
    highlight = "Gracias por hacerlo."
    html = render_email(
        greeting=greeting,
        highlight=highlight,
        cta_label="Volver",
        cta_url=_cta_url("report_thanks"),
        unsubscribe_url=unsubscribe_url,
    )
    sent = _send(user.email, f"¡Gracias {name}! 🙏", html, unsubscribe_url, text=f"{greeting} {highlight}")
    if sent:
        (
            db.query(ExerciseFeedback)
            .filter(ExerciseFeedback.id.in_(feedback_ids))
            .update({"thanks_sent_at": datetime.utcnow()}, synchronize_session=False)
        )
        db.commit()
    return sent


def due_cafecito_efecto_emails(db: DBSession) -> list[tuple[User, dict]]:
    """Los empujes que vencieron y a cuyo donante se le puede contar qué hizo.

    Se mira al VENCER y no al acreditar porque recién ahí el número está cerrado.

    A quién avisarle es la parte delicada: Cafecito no dice quién donó. Lo único
    que ata una donación a una persona es la "intención" que se anota al tocar
    Invitar, y solo sirve si fue la ÚNICA abierta cuando llegó la donación —si
    había varias, cualquiera pudo haber sido—. Ese criterio ya está escrito en
    `boosts.estado_de_donacion` y acá se aplica el mismo: sin donante seguro, no
    se manda nada. Es preferible no agradecer que agradecerle al que no fue.
    """
    from models import GameBoost, GameBoostIntent, GamePlayer

    from game.aforo import SOURCE as AFORO

    ahora = datetime.utcnow()
    # Los de aforo quedan fuera de raíz: no los donó nadie, así que no hay a
    # quién agradecerle. Sin este filtro dependeríamos de que no haya ninguna
    # intención consumida en la ventana de ±5 s del empuje automático — y si la
    # hubiera, el mail le agradecería a quien no fue.
    vencidos = (
        db.query(GameBoost)
        .filter(
            GameBoost.expires_at <= ahora,
            GameBoost.email_sent_at.is_(None),
            GameBoost.source != AFORO,
        )
        .all()
    )

    salida: list[tuple[User, dict]] = []
    for boost in vencidos:
        # Marcar SIEMPRE, aunque no se mande: si no, un empuje sin donante
        # identificable se re-examina en cada corrida para siempre.
        boost.email_sent_at = ahora

        hermanas = (
            db.query(GameBoostIntent)
            .filter(
                GameBoostIntent.consumed_at.isnot(None),
                GameBoostIntent.consumed_at >= boost.created_at - timedelta(seconds=5),
                GameBoostIntent.consumed_at <= boost.created_at + timedelta(seconds=5),
            )
            .all()
        )
        if len(hermanas) != 1:
            continue  # ambiguo: no se puede afirmar quién donó
        jugador = db.get(GamePlayer, hermanas[0].player_id)
        if jugador is None or jugador.user_id is None:
            continue  # donó sin cuenta: no hay a dónde mandarle el mail
        user = db.get(User, jugador.user_id)
        if user is None or user.email_unsubscribed:
            continue

        extra, estudiantes = _efecto_del_empuje(db, boost)
        salida.append(
            (user, {"university": boost.university, "xp_extra": extra, "estudiantes": estudiantes})
        )
    db.commit()
    return salida


def _efecto_del_empuje(db: DBSession, boost) -> tuple[int, int]:
    """Cuánta XP extra puso ese empuje, y entre cuántos estudiantes.

    Sale de `answers.xp_from_boost`, que es justamente el dato que no se puede
    reconstruir después. Ojo con lo que este número ES: la XP que el empuje de
    ESA universidad puso en esa ventana, no la que puso una donación puntual —
    los cafecitos de la ventana se suman y el global se mezcla con el dirigido,
    así que atribuir por donante es imposible por construcción.
    """
    from models import Answer, Enrollment

    q = (
        db.query(
            func.coalesce(func.sum(Answer.xp_from_boost), 0),
            func.count(func.distinct(Answer.user_id)),
        )
        .filter(
            Answer.answered_at >= boost.created_at,
            Answer.answered_at <= boost.expires_at,
            Answer.xp_from_boost > 0,
        )
    )
    if boost.university:
        q = q.filter(
            Answer.user_id.in_(
                db.query(Enrollment.user_id).filter(Enrollment.university == boost.university)
            )
        )
    extra, estudiantes = q.one()
    return int(extra or 0), int(estudiantes or 0)


# Cuántos reclutas entran en el listado del mail. El total de arriba los cuenta a
# TODOS: recortar la tabla no puede recortar el número que la encabeza.
_RECLUTAS_LISTADOS = 20


class ResumenDeReclutas(NamedTuple):
    """Un mail semanal listo para mandar, con lo que hay que anotar si sale."""

    user: User
    datos: dict
    reclutas_a_marcar: tuple[int, ...]


def due_reclutas_semanal_emails(db: DBSession) -> list[ResumenDeReclutas]:
    """Los resúmenes semanales de reclutas que corresponde mandar hoy.

    Solo a quien tuvo movimiento: una semana sin nada no se manda. El mail existe
    para traer buenas noticias, y mandarlo vacío convierte en ruido un canal que
    se usa una vez por semana.

    "Movimiento" es XP NUEVA, no acumulada. `referral_xp_given` no baja nunca, así
    que preguntar por el total mandaba el mismo número todas las semanas y la
    guarda de arriba no se activaba jamás: apenas alguien tenía un recluta, ya
    tenía uno para siempre. Lo que decide es la diferencia contra
    `referral_xp_email_seen`, que se mueve recién cuando el mail sale.
    """
    import xp_boost

    hoy = datetime.utcnow().date()
    hace_una_semana = hoy - timedelta(days=7)

    candidatos = (
        db.query(User)
        .filter(
            User.email_unsubscribed.is_(False),
            or_(
                User.reclutas_email_sent_on.is_(None),
                User.reclutas_email_sent_on <= hace_una_semana,
            ),
        )
        .all()
    )

    salida: list[ResumenDeReclutas] = []
    for user in candidatos:
        propio = db.query(GamePlayer).filter(GamePlayer.user_id == user.id).first()
        if propio is None:
            continue
        se_movieron = (
            db.query(
                User.id,
                User.username,
                User.referral_xp_given,
                User.referral_xp_email_seen,
            )
            .filter(
                User.referred_by_player_id == propio.id,
                User.referral_xp_given > User.referral_xp_email_seen,
            )
            .all()
        )
        if not se_movieron:
            continue
        aportes = sorted(
            ((r.id, r.username, r.referral_xp_given - r.referral_xp_email_seen)
             for r in se_movieron),
            key=lambda t: (-t[2], t[0]),
        )
        total = sum(x for _, _, x in aportes)
        if total <= 0:
            continue
        listados = aportes[:_RECLUTAS_LISTADOS]
        universidades = xp_boost.universidades_de(db, [i for i, _, _ in listados])
        salida.append(
            ResumenDeReclutas(
                user=user,
                datos={
                    "xp_semana": total,
                    "filas": [
                        (u or "", universidades.get(i, ""), x) for i, u, x in listados
                    ],
                },
                reclutas_a_marcar=tuple(i for i, _, _ in aportes),
            )
        )
    return salida


def run_lifecycle_emails(db: DBSession) -> dict:
    bounce_sent = 0
    for user in due_bounce_emails(db):
        if send_bounce_email(db, user):
            bounce_sent += 1

    winback_sent = 0
    for user, _last_finished_at in due_winback_emails(db):
        if send_winback_email(db, user):
            winback_sent += 1

    winback_dx_sent = 0
    for user, jugador in due_winback_dx_emails(db):
        if send_winback_dx_email(db, user, jugador):
            winback_dx_sent += 1

    streak_tier_sent = 0
    to_send, to_mark = due_streak_tier_emails(db)
    for user, tier in to_mark:
        user.streak_email_sent_tier = tier
    if to_mark:
        db.commit()
    for user, tier in to_send:
        if send_streak_tier_email(db, user, tier):
            streak_tier_sent += 1

    cafecito_efecto_sent = 0
    for user, datos in due_cafecito_efecto_emails(db):
        if send_cafecito_efecto_email(db, user, **datos):
            cafecito_efecto_sent += 1

    reclutas_sent = 0
    hoy = datetime.utcnow().date()
    for resumen in due_reclutas_semanal_emails(db):
        if not send_reclutas_semanal_email(db, resumen.user, **resumen.datos):
            continue
        # Commit por destinatario, no al final del lote: las marcas dicen "esta
        # XP ya se contó", y si el proceso se muere a mitad de una tanda con
        # todas las marcas en memoria, los mails que ya salieron vuelven a salir
        # la semana que viene con el mismo número.
        resumen.user.reclutas_email_sent_on = hoy
        db.query(User).filter(User.id.in_(resumen.reclutas_a_marcar)).update(
            {User.referral_xp_email_seen: User.referral_xp_given},
            synchronize_session=False,
        )
        db.commit()
        reclutas_sent += 1

    report_thanks_sent = 0
    for user, feedback_ids in due_report_thanks_emails(db):
        if send_report_thanks_email(db, user, feedback_ids):
            report_thanks_sent += 1

    return {
        "bounce_sent": bounce_sent,
        "winback_sent": winback_sent,
        "winback_dx_sent": winback_dx_sent,
        "streak_tier_sent": streak_tier_sent,
        "report_thanks_sent": report_thanks_sent,
        "cafecito_efecto_sent": cafecito_efecto_sent,
        "reclutas_sent": reclutas_sent,
    }
