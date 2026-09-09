import asyncio
import hmac
import json
import os
import re
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# Load environment variables from .env file
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

sys.path.insert(0, str(Path(__file__).parent.parent))

# Referencia para medir cuánto tarda el proceso en estar listo (ver _seed_blocking).
_PROCESS_START = time.perf_counter()

from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

import emoji_tree
import xp_boost
from game import boosts as game_boosts
from game import cafecito_stream as game_cafecito
from session_store import get_user_progress_db
from database import SessionLocal
from auth import (
    ClerkClaims,
    UserProvisioningError,
    UserResponse,
    _extract_email_and_name,
    get_or_create_user_from_clerk,
    verify_clerk_token,
)
from clerk_webhook import WebhookVerificationError, verify_svix_signature
from models import User, Enrollment, Answer, GamePlayer, UnitState
from sqlalchemy import and_ as sa_and, case, func, or_ as sa_or, select
from sqlalchemy.exc import IntegrityError
from schemas import (
    DueGameNotification,
    BoostTramo,
    RecruitEntry,
    RecruitsResponse,
    AnswerResponse,
    DueNotification,
    EmailRunResponse,
    EmojiStateResponse,
    EnrollmentResponse,
    FeedbackRequest,
    HealthResponse,
    LeaderboardEntry,
    LeaderboardMe,
    LeaderboardResponse,
    LeaderboardSummaryResponse,
    ActiveCapRequest,
    CapPreviewResponse,
    CourseResetResponse,
    NotificationSettings,
    PracticeStatsResponse,
    PublicUniversityLeaderboardResponse,
    PublicUniversityStat,
    SessionStartResponse,
    TopicActionRequest,
    SessionSummaryResponse,
    SimpleResponse,
    SweepAbandonedResponse,
    UniversityLeaderboardResponse,
    UniversityRankRow,
    UserProgressResponse,
    UserStatusResponse,
)

def _seed_blocking() -> None:
    """
    Seed course content from backend/content/.

    Idempotent upsert — safe to run on each deploy. Alembic handles schema;
    this only touches editable content (courses, belt_info, exercises).

    Corre en un worker thread (ver `lifespan`). Se traga sus excepciones a
    propósito: antes esto colgaba del startup sin try/except, así que un JSON
    inválido tumbaba el arranque entero y el deploy quedaba caído. Ahora la app
    sube igual y sigue sirviendo el contenido de la corrida anterior — por eso
    el log de fallo tiene que ser ruidoso y fácil de grepear.
    """
    from seed_content import seed_all

    t0 = time.perf_counter()
    db = SessionLocal()
    try:
        # prune=True: la tabla `exercises` es un espejo de backend/content/. Sin
        # esto, recortar contenido dejaba las filas viejas para siempre y se
        # seguían sirviendo — análisis llegó a tener 460 ejercicios fantasma de
        # una ronda anterior. El prune trae su propia rejilla: se aborta solo si
        # dejaría un ítem declarado sin ejercicios (ver seed_exercises).
        seed_all(db, prune=True)
        print(
            f"[seed] OK en {time.perf_counter() - t0:.2f}s "
            f"(T+{time.perf_counter() - _PROCESS_START:.2f}s desde el import)",
            flush=True,
        )
    except Exception:
        db.rollback()
        print("[seed] FAILED — la app sigue arriba con el contenido anterior", flush=True)
        traceback.print_exc()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Arranca el seed en segundo plano y deja que uvicorn atienda ya.

    El seed hidrata ~3.300 ejercicios desde Postgres y antes corría dentro del
    handler de startup, así que /health y todo lo demás quedaban en cola hasta
    que terminaba. Esa ventana de cold start se comía las requests del alta:
    quien volvía del login de Google justo durante un deploy no llegaba a
    crear su fila.

    `asyncio.to_thread` y no una llamada directa: el seed es SQLAlchemy
    síncrono y correrlo en el event loop bloquea todo igual que antes.

    No hace falta un gate de readiness. `seed_one_course` hace un único commit
    al final, prune incluido, así que bajo MVCC ninguna conexión ve un estado a
    medio actualizar: se sirve el contenido viejo hasta el commit y el nuevo
    después. Si alguna vez se parte ese commit en varios por performance, esta
    garantía se rompe en silencio.
    """
    # Los navegadores reportan alias IANA viejos ("America/Buenos_Aires", el
    # nombre que usa ICU/CLDR) y los validamos y guardamos tal cual. Si la
    # imagen trae una tzdata sin esos links, la validación rechaza a todos los
    # usuarios y el loop de notificaciones los saltea en silencio (pasó el
    # 19/8/2026 con un rebuild de Railway). Mejor que el deploy falle acá y
    # siga sirviendo el contenedor anterior.
    #
    # Va en el lifespan y no adentro de _seed_blocking: ese corre en un thread
    # que se traga las excepciones, así que ahí el chequeo no podría voltear
    # el arranque, que es justamente para lo que existe.
    try:
        ZoneInfo("America/Buenos_Aires")
    except ZoneInfoNotFoundError:
        raise RuntimeError(
            "tzdata sin links de compatibilidad IANA: falta el paquete pip "
            "`tzdata` (ver requirements.txt)"
        )

    task = asyncio.create_task(asyncio.to_thread(_seed_blocking))
    # Guardar la referencia: sin esto el GC puede llevarse el task y con él la
    # excepción, y el fallo del seed pasaría inadvertido.
    app.state.seed_task = task

    # El oyente de Cafecito: escucha las donaciones y aplica el empuje solo, en
    # el momento (game/cafecito_stream.py). Sin CAFECITO_STREAM_TOKEN se apaga
    # solo, así que en desarrollo no hace nada.
    #
    # Thread propio y no una tarea del event loop: adentro hay un socket
    # sincrónico y SQLAlchemy, que en el loop bloquearían todo. Daemon, para que
    # no impida que el proceso termine si el apagado ordenado no llega a correr.
    parar_cafecito = threading.Event()
    hilo_cafecito = threading.Thread(
        target=game_cafecito.escuchar,
        args=(parar_cafecito,),
        name="cafecito-stream",
        daemon=True,
    )
    hilo_cafecito.start()
    app.state.cafecito_stop = parar_cafecito

    yield
    parar_cafecito.set()
    task.cancel()


app = FastAPI(title="Intervalo Backend", lifespan=lifespan)

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Minijuego «derivemos»: bounded context completo en backend/game/ (primer
# APIRouter del repo; el resto de main.py sigue con @app.* directo).
from game.router import router as game_router  # noqa: E402

app.include_router(game_router)


# ── Dependency functions ──────────────────────────────────────────────────────

def get_db():
    """Get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    authorization: str = Header(None),
    db: Session = Depends(get_db)
) -> User:
    """
    Resolve the authenticated user from a Clerk session JWT.

    Clerk issues the token on the frontend; we verify the signature against
    Clerk's JWKS and JIT-provision a local `User` row on first sight.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")

    # Extract token from "Bearer <token>"
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    try:
        claims = verify_clerk_token(token)
    except RuntimeError as exc:
        # Missing CLERK_* env vars — server misconfiguration, not a client error.
        raise HTTPException(status_code=503, detail=str(exc))

    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    try:
        return get_or_create_user_from_clerk(db, claims)
    except ValueError as exc:
        # e.g. Clerk token has no email and no secret key is configured
        raise HTTPException(status_code=401, detail=str(exc))
    except (UserProvisioningError, IntegrityError) as exc:
        # Carrera perdida contra el otro escritor (webhook o pestaña paralela).
        # 503 y no 500: es transitorio y el cliente lo reintenta.
        db.rollback()
        print(f"[provision] FAILED clerk={claims.sub}: {exc!r}", flush=True)
        raise HTTPException(
            status_code=503, detail="No pudimos crear tu cuenta. Probá de nuevo."
        )


def require_internal_secret(x_internal_secret: str = Header(None)):
    """Guard for worker-facing endpoints — a shared secret, not Clerk."""
    expected = os.environ.get("INTERNAL_API_SECRET")
    if not expected:
        raise HTTPException(status_code=503, detail="INTERNAL_API_SECRET not configured")
    if not hmac.compare_digest(x_internal_secret or "", expected):
        raise HTTPException(status_code=401, detail="Invalid internal secret")


def require_dev_endpoints():
    """Guard de las rutas /dev/*, que son herramientas de QA local.

    Opt-in y no opt-out a propósito: la variable existe solo en la máquina de
    quien testea, así que cualquier entorno que no la declare —producción
    incluida— tiene estas rutas apagadas. Un guard al revés (apagar si es
    prod) dejaría el agujero abierto en cualquier entorno nuevo que alguien
    levante sin acordarse de la variable.

    404 y no 403: una ruta que no existe no le confirma a nadie que exista en
    otro lado."""
    if os.environ.get("ENABLE_DEV_ENDPOINTS") != "1":
        raise HTTPException(status_code=404, detail="Not Found")


# ── Helpers ───────────────────────────────────────────────────────────────────

# ── Pydantic models ───────────────────────────────────────────────────────────

class StartSessionRequest(BaseModel):
    user_name: str
    course: str | None = None


class PracticeSessionItem(BaseModel):
    belt: str
    topic: str


class StartPracticeSessionRequest(BaseModel):
    user_name: str
    items: list[PracticeSessionItem]
    count: int
    course: str | None = None


class TestSessionItem(BaseModel):
    belt: str
    topic: str
    exercise_type: str


class TestFilters(BaseModel):
    has_math: bool = False
    has_graph: bool = False
    has_table: bool = False


class StartTestSessionRequest(BaseModel):
    items: list[TestSessionItem]
    course: str | None = None
    shuffle: bool = True
    filters: TestFilters | None = None


class AnswerRequest(BaseModel):
    session_id: str
    exercise_id: str
    # Identificador estable del ejercicio real (p. ej. "white_definition_clsf_01").
    # El cliente lo reporta para poder auditar exactamente qué ejercicio se sirvió
    # y para avanzar el ciclo de no-repetición por ítem; opcional por
    # compatibilidad con clientes viejos.
    exercise_external_id: str | None = None
    answer_index: int
    attempts: int
    response_time_s: float


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscribeRequest(BaseModel):
    endpoint: str
    keys: PushKeys


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


class NotificationSettingsRequest(BaseModel):
    enabled: bool
    time: str | None = None
    timezone: str | None = None


class PrunePushRequest(BaseModel):
    subscription_ids: list[int]


class DeliveryResult(BaseModel):
    notification_id: int
    # "ok" | "error_<status>" | "error", tal como lo arma el notifier.
    status: str


class PushDeliveryRequest(BaseModel):
    results: list[DeliveryResult]


class TestFeedbackSaveRequest(BaseModel):
    session_id: str
    doc: str


class SessionFeedbackRequest(BaseModel):
    action: str  # "impression" | "answer" | "report"
    session_id: str
    exercise_external_id: str | None = None  # impression / report
    question_type: str | None = None  # "A" | "B" | "D" (impression) — "C" implícito en report
    feedback_id: int | None = None  # answer
    value: str | None = None
    free_text: str | None = None
    reason: str | None = None  # answer, canal D: chip de razón (opcional)


class SessionFeedbackResponse(BaseModel):
    success: bool
    feedback_id: int
    xp_earned: int = 0


# ── Endpoints ─────────────────────────────────────────────────────────────────

# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
def health_check():
    return {"status": "ok"}


# ── Raíz y favicon ────────────────────────────────────────────────────────────
#
# Ninguna de las dos es parte de la API: están para que el log deje de mentir.
# Todo chequeo de vida por defecto —Railway, un uptime robot, alguien que pega
# la URL en el navegador— pega en `/`, y el navegador además pide `/favicon.ico`
# sin que nadie se lo mande. Las dos contestaban 404, así que el arranque
# mostraba errores que no eran errores; y un log con ruido de fondo es un log en
# el que los errores de verdad no se ven.

@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def root():
    """Qué es esto y dónde está lo demás.

    Devuelve el mismo `status` que `/health` en vez de redirigir ahí: un chequeo
    de vida que sigue un 3xx no prueba nada sobre el proceso que contesta al
    final del salto.

    HEAD va declarado a mano y no sale gratis: el `Route` de Starlette agrega
    HEAD solo cuando registra un GET, pero el `APIRoute` de FastAPI no, así que
    un `@app.get` suelto contesta 405 al HEAD —es lo que sigue haciendo
    `/health`—. Y HEAD es justo lo que usan varios chequeos de vida, este
    incluido: el arranque de esta sesión pegó un `HEAD /` antes que cualquier
    GET.
    """
    return {"status": "ok", "service": "intervalo-backend", "docs": "/docs"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """204, no un ícono.

    La API no tiene nada de marca para mostrar. Con 404 el navegador lo vuelve a
    pedir en cada visita; con 204 se da por contestado y deja de insistir.
    """
    return Response(status_code=204)


# ── Authentication ────────────────────────────────────────────────────────────
#
# Sign-in / sign-up is handled by Clerk on the frontend. The backend only
# verifies the resulting session JWT (see `get_current_user` above) and
# surfaces the current user via `/auth/me`. No OAuth redirects live here
# anymore.

@app.get("/auth/me", response_model=UserResponse)
def get_current_user_info(
    current_user: User = Depends(get_current_user)
):
    """Get current authenticated user info."""
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        name=current_user.display_name or current_user.name,
        username=current_user.username,
        display_name=current_user.display_name,
        clerk_user_id=current_user.clerk_user_id,
    )


class UpdateProfileRequest(BaseModel):
    username: str | None = None
    display_name: str | None = None


@app.patch("/user/profile", response_model=UserResponse)
def update_profile(
    body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the user's handle (username) and/or display name (apodo)."""
    from usernames import normalize_username, validate_username

    if body.username is not None:
        candidate = normalize_username(body.username)
        ok, reason = validate_username(candidate)
        if not ok:
            raise HTTPException(status_code=422, detail=reason)
        # Contra el REGISTRO y no contra `users`: el nombre puede ser de un
        # jugador del minijuego —invitado incluido— o estar retirado pero
        # todavía resolviendo links `?r=`. Mirar solo `users` entregaba nombres
        # que ya eran de otra persona. `reclamar` además retira el @ anterior y
        # baja el nuevo a `users.username`, que pasa a ser caché.
        import handles

        jugador = db.query(GamePlayer).filter(GamePlayer.user_id == current_user.id).first()
        try:
            handles.reclamar(
                db,
                candidate,
                user_id=current_user.id,
                player_id=jugador.id if jugador else None,
            )
        except handles.HandleTomado:
            raise HTTPException(status_code=409, detail="Ese usuario ya está en uso.")
        except IntegrityError:
            # El chequeo de arriba es TOCTOU: dos personas pidiendo el mismo @ a
            # la vez llegan las dos hasta acá. La carrera se levanta ACÁ y no en
            # el commit de más abajo porque `reclamar` hace `flush()` adentro
            # (ver handles.py), y sin este except el conflicto salía 500.
            db.rollback()
            raise HTTPException(status_code=409, detail="Ese usuario ya está en uso.")

    if body.display_name is not None:
        current_user.display_name = body.display_name.strip() or None

    try:
        db.commit()
    except IntegrityError:
        # Red de contención: hoy lo único que puede chocar es el @, y ya se
        # resolvió arriba. Queda por si mañana este endpoint escribe algo más.
        db.rollback()
        raise HTTPException(status_code=409, detail="Ese usuario ya está en uso.")
    db.refresh(current_user)

    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        name=current_user.display_name or current_user.name,
        username=current_user.username,
        display_name=current_user.display_name,
        clerk_user_id=current_user.clerk_user_id,
    )


# ── User enrollment ───────────────────────────────────────────────────────────

class EnrollmentRequest(BaseModel):
    university: str
    career: str
    name: str | None = None
    # Curso elegido en el onboarding (slug). None → analisis, por compatibilidad
    # con clientes/datos viejos que no mandaban curso.
    course: str | None = None
    # Motivación: la pregunta se retiró del onboarding, el campo se conserva
    # para no romper el contrato con clientes viejos.
    motivation: str | None = None
    # Unidades declaradas como conocidas, claves del catálogo separadas por coma.
    known_units: str | None = None
    # Atribución de primer contacto, capturada al aterrizar (ver
    # web/src/lib/analytics/attribution.ts). Se persiste una sola vez por usuario.
    first_group_id: str | None = None
    first_utm_source: str | None = None
    # El @ de quien trajo a esta persona, capturado del `?r=` al aterrizar
    # (ver web/src/lib/analytics/attribution.ts). Es el MISMO parámetro que
    # ya usa el alta del minijuego: un solo link sirve para los dos.
    referrer: str | None = None
    # Resultado del ejercicio de prueba del onboarding (primer ítem del curso).
    # True = acertó al primer intento, False = falló alguna vez, None = sin dato.
    intro_item_correct: bool | None = None
    # Cantidad de intentos hasta acertar (o intentos totales si nunca acertó).
    attempts: int | None = None
    # Tiempo total que tardó en responder el ejercicio de prueba, en ms.
    response_time_ms: int | None = None


def _sellar_mudanza(enrollment: Enrollment, nueva: str) -> None:
    """Marca CUÁNDO esta persona se cambió de universidad. Gemelo exacto de la
    guarda de `game/router.py` que hace lo mismo con `game_players`.

    Es lo que sostiene el candado antimudanza del empuje: `boosts.aplica_el_empuje`
    da por bueno el empuje cuando el sello está en NULL, así que una columna que
    nadie escribe es un candado que no existe. Sin esto, con empujes de 24-48 h
    alcanzaba un `POST /user/enroll` apuntando a la universidad impulsada para
    cobrar el multiplicador.

    Solo la MUDANZA, no la primera carga: cargar la universidad por primera vez
    no puede costarte el empuje que está corriendo. Y "primera carga" es no
    tenerla Y no haberla tenido nunca, o vaciarla y volver a cargarla devolvería
    el sello a NULL — el mismo camino que ya se cerró del lado del juego.
    """
    from datetime import datetime

    primera_carga = not enrollment.university and enrollment.university_set_at is None
    if not primera_carga and nueva != enrollment.university:
        enrollment.university_set_at = datetime.utcnow()


@app.post("/user/enroll", response_model=EnrollmentResponse)
def enroll_user(
    body: EnrollmentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Enroll user in a course with onboarding data."""
    from models import Enrollment
    from universities import canonical_university

    # Curso elegido en el onboarding (slug → id). Default analisis para compat.
    course_id = _resolve_course_id(body.course, db)

    # "Uba", "uba" y "Universidad de Buenos Aires" son la misma universidad: se
    # guarda siempre la sigla. Sin esto cada variante es una institución aparte
    # para el ranking por universidad y pierde su tag en el leaderboard.
    university = canonical_university(body.university) or ""

    # Check if already enrolled
    existing = db.query(Enrollment).filter(
        Enrollment.user_id == current_user.id,
        Enrollment.course_id == course_id,
    ).first()

    if existing:
        # Update enrollment
        _sellar_mudanza(existing, university)
        existing.university = university
        existing.career = body.career
        existing.motivation = body.motivation
        existing.known_units = body.known_units
    else:
        # Create new enrollment
        enrollment = Enrollment(
            user_id=current_user.id,
            course_id=course_id,
            university=university,
            career=body.career,
            motivation=body.motivation,
            known_units=body.known_units,
        )
        db.add(enrollment)

    # Save display name from tutorial
    if body.name:
        current_user.display_name = body.name

    # Atribución de primer contacto: gana el primer valor que se haya guardado,
    # nunca se pisa. El cliente ya aplica la misma regla de su lado
    # (register_once), pero repetirla acá la vuelve independiente de que el
    # localStorage se haya limpiado entre el aterrizaje y el alta.
    #
    # El id se valida contra el mismo formato que usa el cliente (prefijo de
    # universidad + número); lo que no matchee se descarta en vez de guardarse,
    # así un parámetro basureado no se convierte en una cohorte fantasma.
    if current_user.first_group_id is None and body.first_group_id:
        if re.fullmatch(r"[a-z]{2,6}\d{1,5}", body.first_group_id):
            current_user.first_group_id = body.first_group_id
    if current_user.first_utm_source is None and body.first_utm_source:
        if re.fullmatch(r"[a-z]{2,20}", body.first_utm_source):
            current_user.first_utm_source = body.first_utm_source

    # Quién trajo a esta persona. Mismo criterio write-once que la atribución de
    # arriba —quien te trajo te trajo una vez— y la misma validación de formato
    # que el cliente, para que un parámetro basureado no cree una arista rara.
    #
    # `anotar_usuario` se encarga de las guardas contra autoreclutarse, que acá
    # importan de verdad: en el alta la mayoría todavía no tiene fila de jugador,
    # así que el `salvo` de siempre sale en None.
    if body.referrer and re.fullmatch(r"[a-z0-9._]{1,20}", body.referrer):
        import referrals

        referrals.anotar_usuario(db, current_user, body.referrer)

    try:
        db.commit()
    except IntegrityError:
        # El cliente reintentó un POST que en realidad había llegado: el
        # UNIQUE (user_id, course_id) rebota el segundo INSERT. Sin esto, el
        # retry del onboarding le devolvía un 500 al usuario.
        db.rollback()
        winner = db.query(Enrollment).filter(
            Enrollment.user_id == current_user.id,
            Enrollment.course_id == course_id,
        ).first()
        if winner is None:
            raise
        _sellar_mudanza(winner, university)
        winner.university = university
        winner.career = body.career
        winner.motivation = body.motivation
        winner.known_units = body.known_units
        if body.name:
            current_user.display_name = body.name
        db.commit()
        # Sin seed_intro_item: el intento ganador ya sembró el ítem si
        # correspondía, y acá `existing` era falso solo por la carrera.
        return {"success": True, "message": "Enrollment successful"}

    # ¿Fue la persona número 10 de su universidad hoy? Solo en un alta NUEVA:
    # un re-enrollment no trae a nadie. Va después del commit para que el alta
    # ya esté contada por `personas_nuevas_hoy` —si no, la décima persona se
    # cuenta a sí misma como novena y el empuje se corre a la siguiente— y para
    # que un problema acá no pueda tirar abajo el alta, que ya está guardada.
    if not existing:
        from game import aforo as game_aforo

        if game_aforo.revisar(db, university) is not None:
            db.commit()

    # Solo en una alta nueva: persistir el resultado del ejercicio de prueba del
    # onboarding sobre el primer ítem del curso (acierto → mañana, fuera de la 1ª
    # sesión; fallo → hoy, dentro). En re-enrollment no se toca el progreso.
    if not existing and body.intro_item_correct is not None:
        from session_store import seed_intro_item
        seed_intro_item(
            current_user.id,
            course_id,
            body.intro_item_correct,
            db,
            attempts=body.attempts,
            response_time_ms=body.response_time_ms,
        )

    return {
        "success": True,
        "message": "Enrollment successful",
    }


@app.get("/user/status", response_model=UserStatusResponse)
def get_user_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Authoritative new-vs-returning check, read from the DB.

    A returning user has an enrollment and/or learning state, regardless of
    what their Clerk `onboarded` metadata says. The frontend uses this to
    decide whether to run onboarding or send the user straight to the dashboard.

    Ninguno de los dos chequeos filtra por curso: antes miraban solo
    course_id=1 y perdían a usuarios enrolados/con progreso únicamente en
    otro curso (ej. álgebra), a quienes se les volvía a pedir universidad/
    carrera pese a tenerlas cargadas.
    """
    from models import Enrollment, UnitState

    enrolled = db.query(Enrollment.id).filter(
        Enrollment.user_id == current_user.id,
    ).first() is not None

    has_progress = db.query(UnitState.id).filter(
        UnitState.user_id == current_user.id,
    ).first() is not None

    return UserStatusResponse(enrolled=enrolled, has_progress=has_progress)


@app.get("/user/progress", response_model=UserProgressResponse)
def get_user_progress(
    tz: str | None = Query(default=None),
    course: str | None = Query(default=None),
    pwa: bool | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get user's current progress (topic states and level info).

    `tz` es la zona horaria IANA del navegador; si es válida, la persistimos en el
    usuario para que el "día" de la repetición espaciada use su zona, no la del
    servidor (UTC). El home llama a este endpoint en cada carga, así queda fresca.

    `course` (opcional) es el slug del curso a filtrar. Si no viene, se usa el
    curso por defecto (id=1, "analisis").

    `pwa` es si el cliente está corriendo en display-mode: standalone (ver
    web/src/lib/platform/detect.ts :: isStandalone()). Se persiste una sola vez.
    """
    # Este endpoint es el que llama el home en cada carga, así que llegar acá
    # ES haber llegado al home. Es el escalón del embudo que separa "se trabó
    # en la autenticación" de "llegó a la app y no tocó empezar" (ver
    # User.reached_home). Se escribe una sola vez, no en cada carga.
    if not current_user.reached_home:
        current_user.reached_home = True
        db.commit()

    # Mismo criterio: se escribe la primera vez que llega en true, nunca se pisa.
    if pwa and current_user.pwa_first_seen_at is None:
        from datetime import datetime
        current_user.pwa_first_seen_at = datetime.utcnow()
        db.commit()

    if tz and tz != current_user.timezone:
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            pass
        else:
            current_user.timezone = tz
            db.commit()
    try:
        from models import Course
        if course:
            course_row = db.query(Course).filter(Course.slug == course).first()
            if course_row is None:
                raise HTTPException(status_code=404, detail=f"Curso '{course}' no encontrado")
            course_id = course_row.id
        else:
            course_id = 1  # Default course
        return get_user_progress_db(current_user.id, course_id, db)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


def _resolve_course_id(course: str | None, db: Session) -> int:
    """Resuelve el id de curso desde el slug; default = curso id=1 (analisis)."""
    if not course:
        return 1
    from models import Course
    course_row = db.query(Course).filter(Course.slug == course).first()
    if course_row is None:
        raise HTTPException(status_code=404, detail=f"Curso '{course}' no encontrado")
    return course_row.id


@app.get("/user/practice-stats", response_model=PracticeStatsResponse)
def get_practice_stats(
    course: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stats del usuario para un curso en la iteración vigente, SOLO modo práctica:
    sesiones de práctica completadas y ejercicios acertados en ellas."""
    from models import Session as SessionModel
    from session_store import _get_course_progress
    course_id = _resolve_course_id(course, db)
    iteration = _get_course_progress(current_user.id, course_id, db).iteration

    sessions_completed = db.query(func.count(SessionModel.id)).filter(
        SessionModel.user_id == current_user.id,
        SessionModel.course_id == course_id,
        SessionModel.iteration == iteration,
        SessionModel.mode == "practice",
        SessionModel.finished_at.isnot(None),
    ).scalar()

    answered, exercises_correct = db.query(
        func.count(Answer.id),
        func.count().filter(Answer.is_correct.is_(True)),
    ).join(
        SessionModel, Answer.session_id == SessionModel.id,
    ).filter(
        Answer.user_id == current_user.id,
        Answer.course_id == course_id,
        Answer.iteration == iteration,
        SessionModel.mode == "practice",
    ).one()

    return PracticeStatsResponse(
        sessions_completed=sessions_completed or 0,
        exercises_answered=answered or 0,
        exercises_correct=exercises_correct or 0,
    )


# ── Editor de curso ───────────────────────────────────────────────────────────

@app.post("/course/{course}/topic/advance", response_model=UserProgressResponse)
def course_topic_advance(
    course: str,
    body: TopicActionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Adelantar un tema: lo desbloquea fuera de orden (o lo reactiva si estaba
    suspendido). Devuelve el progreso actualizado del curso."""
    from session_store import advance_topic, get_user_progress_db
    course_id = _resolve_course_id(course, db)
    advance_topic(current_user.id, course_id, body.belt, body.topic, db)
    return get_user_progress_db(current_user.id, course_id, db)


@app.post("/course/{course}/topic/suspend", response_model=UserProgressResponse)
def course_topic_suspend(
    course: str,
    body: TopicActionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Suspender un tema: lo oculta del home y cede su cupo a los temas siguientes."""
    from session_store import suspend_topic, get_user_progress_db
    course_id = _resolve_course_id(course, db)
    suspend_topic(current_user.id, course_id, body.belt, body.topic, db)
    return get_user_progress_db(current_user.id, course_id, db)


@app.post("/course/{course}/topic/reset", response_model=UserProgressResponse)
def course_topic_reset(
    course: str,
    body: TopicActionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reiniciar un tema: sus ítems vuelven a 'nuevo' y reingresan a repaso."""
    from session_store import reset_topic, get_user_progress_db
    course_id = _resolve_course_id(course, db)
    reset_topic(current_user.id, course_id, body.belt, body.topic, db)
    return get_user_progress_db(current_user.id, course_id, db)


@app.get("/course/{course}/active-cap/preview", response_model=CapPreviewResponse)
def course_active_cap_preview(
    course: str,
    value: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sin aplicar: qué temas se desbloquean/re-bloquean al fijar el cap en `value`."""
    from session_store import cap_change_preview
    course_id = _resolve_course_id(course, db)
    return cap_change_preview(current_user.id, course_id, value, db)


@app.put("/course/{course}/active-cap", response_model=UserProgressResponse)
def course_set_active_cap(
    course: str,
    body: ActiveCapRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fija cuántos ítems puede tener en aprendizaje a la vez (clamp 1..total)."""
    from session_store import set_active_cap, get_user_progress_db
    course_id = _resolve_course_id(course, db)
    set_active_cap(current_user.id, course_id, body.value, db)
    return get_user_progress_db(current_user.id, course_id, db)


@app.put("/course/{course}/session-size", response_model=UserProgressResponse)
def course_set_session_size(
    course: str,
    body: ActiveCapRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fija el máximo de ejercicios por sesión de repaso (clamp 1..30)."""
    from session_store import set_session_size, get_user_progress_db
    course_id = _resolve_course_id(course, db)
    set_session_size(current_user.id, course_id, body.value, db)
    return get_user_progress_db(current_user.id, course_id, db)


@app.post("/course/{course}/reset", response_model=CourseResetResponse)
def course_reset(
    course: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reiniciar el curso: archiva el progreso vigente y arranca una iteración
    nueva (el cinturón refleja solo la iteración vigente)."""
    from session_store import reset_course
    course_id = _resolve_course_id(course, db)
    iteration = reset_course(current_user.id, course_id, db)
    return CourseResetResponse(iteration=iteration)


# ── Push notifications ──────────────────────────────────────────────────────────

@app.post("/push/subscribe", response_model=SimpleResponse)
def push_subscribe(
    body: PushSubscribeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store a browser PushSubscription for the current user."""
    import push_store

    push_store.upsert_subscription(
        db, current_user.id, body.endpoint, body.keys.p256dh, body.keys.auth
    )
    return {"success": True}


@app.delete("/push/subscribe", response_model=SimpleResponse)
def push_unsubscribe(
    body: PushUnsubscribeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a browser PushSubscription (called when the user unsubscribes)."""
    import push_store

    push_store.delete_subscription(db, current_user.id, body.endpoint)
    return {"success": True}


@app.get("/push/diagnostic", response_model=SimpleResponse)
def push_diagnostic_beacon(
    event: str,
    error: str | None = None,
    endpoint: str | None = None,
    raw_len: str | None = None,
    ua: str | None = None,
    notification_id: int | None = None,
    # De qué producto viene el aviso. "dx" = minijuego, cualquier otra cosa =
    # Intervalo. Hace falta porque los envíos del juego viven en su propia tabla
    # con su propio espacio de ids: sin este campo, un click en un aviso de dx
    # marcaría como abierta la fila de otra persona en `notification_sends`.
    app: str | None = None,
    db: Session = Depends(get_db),
):
    """GET twin of push_diagnostic, reporting on EVERY push the service
    worker receives (not just decode failures) — see sw.js's `beacon()`. A
    plain GET with no custom headers is a CORS "simple request" (no
    preflight), so it's the fallback channel in case the POST version's
    JSON body / Content-Type ever gets blocked in the service worker's
    fetch context — we had zero of those land while chasing a recurring
    generic-fallback bug with no other client-side signal at all.

    event="click" doubles as the "notification opened" signal (see sw.js
    notificationclick): persists NotificationSend.opened_at so effectiveness
    can be analyzed later per category/variant."""
    import logging

    import push_store

    user_id = push_store.user_id_for_endpoint(db, endpoint) if endpoint else None
    logging.warning(
        "push beacon event=%s user=%s endpoint=...%s error=%s raw_len=%s ua=%s",
        event,
        user_id,
        (endpoint or "")[-24:],
        error,
        raw_len,
        ua,
    )
    if event == "click" and notification_id is not None:
        try:
            if app == "dx":
                from game import notifications as avisos_del_juego

                avisos_del_juego.mark_notification_opened(
                    db, notification_id, endpoint or ""
                )
            else:
                push_store.mark_notification_opened(notification_id, endpoint, db)
        except Exception:
            logging.exception("failed to mark notification %s opened", notification_id)
    return {"success": True}


@app.get("/user/notification-settings", response_model=NotificationSettings)
def get_notification_settings(
    current_user: User = Depends(get_current_user),
):
    import push_store

    return push_store.get_settings(current_user)


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):(00|15|30|45)$")


@app.put("/user/notification-settings", response_model=NotificationSettings)
def put_notification_settings(
    body: NotificationSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    import push_store

    if body.enabled:
        if not body.time or not _TIME_RE.match(body.time):
            raise HTTPException(status_code=400, detail="time must be HH:MM in 15-min steps")
        if not body.timezone:
            raise HTTPException(status_code=400, detail="timezone is required")
        try:
            ZoneInfo(body.timezone)
        except ZoneInfoNotFoundError:
            raise HTTPException(status_code=400, detail="invalid timezone")

    return push_store.save_settings(
        db, current_user, body.enabled, body.time, body.timezone
    )


@app.get(
    "/internal/notifications/due",
    response_model=list[DueNotification],
    dependencies=[Depends(require_internal_secret)],
)
def internal_due_notifications(
    force: bool = False,
    db: Session = Depends(get_db),
):
    """Worker-facing: users to notify right now (claims them in-transaction)."""
    import push_store

    return push_store.due_notifications(db, force=force)


@app.get(
    "/internal/notifications/events",
    response_model=list[DueNotification],
    dependencies=[Depends(require_internal_secret)],
)
def internal_due_event_notifications(
    force: bool = False,
    db: Session = Depends(get_db),
):
    """Worker-facing: los avisos DE EVENTO listos para mandar (los reclama).

    Endpoint aparte del de la notificación normal a propósito: tienen cupos
    distintos y disparadores distintos, y mezclarlos haría que un tick que falla
    en uno arrastre al otro."""
    import push_store

    return push_store.due_event_notifications(db, force=force)


@app.post(
    "/internal/push/prune",
    response_model=SimpleResponse,
    dependencies=[Depends(require_internal_secret)],
)
def internal_prune_push(
    body: PrunePushRequest,
    db: Session = Depends(get_db),
):
    """Worker-facing: drop subscriptions that returned 404/410."""
    import push_store

    push_store.delete_subscriptions_by_id(db, body.subscription_ids)
    return {"success": True}


@app.post(
    "/internal/push/delivery",
    response_model=SimpleResponse,
    dependencies=[Depends(require_internal_secret)],
)
def internal_push_delivery(
    body: PushDeliveryRequest,
    db: Session = Depends(get_db),
):
    """Worker-facing: qué devolvió el push service para cada envío del tick.

    La fila de notification_sends se crea al elegir el copy, antes de intentar
    mandar, así que sin este reporte un envío fallido queda indistinguible de uno
    exitoso."""
    import push_store

    push_store.record_delivery_results(
        db, [(r.notification_id, r.status) for r in body.results]
    )
    return {"success": True}


# ── Avisos del minijuego ─────────────────────────────────────────────────────
#
# Cuatro endpoints propios en vez de reusar los de arriba, y no es simetría
# gratuita: las filas del juego viven en `game_notification_sends` y
# `game_push_subscriptions`, con su propio espacio de ids. Con un solo endpoint
# de entrega, un `notification_id` podría referirse a dos filas distintas según
# de qué tanda viniera.
#
# La FORMA de la respuesta sí es la misma, y eso es lo que permite que el
# notifier use la misma `correrTanda` para las cuatro rutas.


@app.get(
    "/internal/notifications/game",
    response_model=list[DueGameNotification],
    dependencies=[Depends(require_internal_secret)],
)
def internal_due_game_notifications(
    force: bool = False,
    db: Session = Depends(get_db),
):
    """Worker-facing: el aviso programado del juego, reclamado en la transacción."""
    from game import notifications as avisos

    return avisos.due_game_notifications(db, force=force)


@app.get(
    "/internal/notifications/game-events",
    response_model=list[DueGameNotification],
    dependencies=[Depends(require_internal_secret)],
)
def internal_due_game_event_notifications(
    force: bool = False,
    db: Session = Depends(get_db),
):
    """Worker-facing: los avisos reactivos del juego (cafecito, reclutas, ranking)."""
    from game import notifications as avisos

    return avisos.due_game_event_notifications(db, force=force)


@app.post(
    "/internal/push/game-prune",
    response_model=SimpleResponse,
    dependencies=[Depends(require_internal_secret)],
)
def internal_prune_game_push(
    body: PrunePushRequest,
    db: Session = Depends(get_db),
):
    from game import notifications as avisos

    avisos.delete_subscriptions_by_id(db, body.subscription_ids)
    return {"success": True}


@app.post(
    "/internal/push/game-delivery",
    response_model=SimpleResponse,
    dependencies=[Depends(require_internal_secret)],
)
def internal_game_push_delivery(
    body: PushDeliveryRequest,
    db: Session = Depends(get_db),
):
    from game import notifications as avisos

    avisos.record_delivery_results(
        db, [(r.notification_id, r.status) for r in body.results]
    )
    return {"success": True}


# ── Lifecycle emails ─────────────────────────────────────────────────────────────

@app.post(
    "/internal/emails/run",
    response_model=EmailRunResponse,
    dependencies=[Depends(require_internal_secret)],
)
def internal_run_lifecycle_emails(db: Session = Depends(get_db)):
    """Worker-facing: send due bounce/win-back emails now (best-effort per user)."""
    import lifecycle_emails

    return lifecycle_emails.run_lifecycle_emails(db)


@app.post(
    "/internal/sessions/sweep-abandoned",
    response_model=SweepAbandonedResponse,
    dependencies=[Depends(require_internal_secret)],
)
def internal_sweep_abandoned_sessions(db: Session = Depends(get_db)):
    """Worker-facing: marcar como abandonadas las sesiones que quedaron abiertas.

    El abandono no se puede detectar en el momento (nadie avisa que se fue), así
    que se barre por tiempo. Ver session_store.sweep_abandoned_sessions."""
    import session_store

    return {"marked": session_store.sweep_abandoned_sessions(db)}


@app.post("/webhooks/clerk", include_in_schema=False)
async def clerk_webhook(
    request: Request,
    svix_id: str = Header(None, alias="svix-id"),
    svix_timestamp: str = Header(None, alias="svix-timestamp"),
    svix_signature: str = Header(None, alias="svix-signature"),
    db: Session = Depends(get_db),
):
    """Crea la fila de `users` apenas Clerk confirma el alta.

    Sin esto, la fila nacía en la primera request autenticada del navegador y
    se perdía el 15% de las cuentas: quien cerraba la pestaña volviendo del
    login quedaba en Clerk y no acá. Con el webhook la persona existe aunque
    el celular se cuelgue, y entra al circuito de emails como cualquier otra.

    Sin autenticación de Clerk ni secreto interno: acá la credencial es la
    firma de Svix. Fuera del schema porque el frontend nunca lo llama.

    Nada de 5xx por fallos de negocio — Svix reintenta con backoff durante un
    día entero y una respuesta así arma una tormenta de reintentos. Solo se
    deja subir un 500 cuando reintentar de verdad puede ayudar (la base caída).
    """
    body = await request.body()
    try:
        verify_svix_signature(
            body=body,
            svix_id=svix_id,
            svix_timestamp=svix_timestamp,
            svix_signature=svix_signature,
        )
    except WebhookVerificationError as exc:
        reason = str(exc)
        if "no configurado" in reason:
            # Config faltante: 503 para que Svix reintente y el evento se
            # recupere solo cuando se cargue la variable.
            raise HTTPException(status_code=503, detail=reason)
        if "faltan headers" in reason:
            raise HTTPException(status_code=400, detail=reason)
        raise HTTPException(status_code=401, detail=reason)

    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="body no es JSON")

    event_type = payload.get("type")
    if event_type not in ("user.created", "user.updated"):
        return {"ignored": event_type}

    user_data = payload.get("data") or {}
    clerk_id = user_data.get("id")
    if not clerk_id:
        return {"ignored": "sin id de usuario"}

    def _provision():
        email, name = _extract_email_and_name(user_data)
        if not email:
            # Alta sin email resuelto (flujos de teléfono, verificación
            # pendiente). No es un error nuestro y reintentar no lo arregla:
            # cuando la persona entre, el camino del request la crea.
            print(f"[provision] webhook sin email para {clerk_id}, se ignora", flush=True)
            return None
        claims = ClerkClaims(sub=clerk_id, email=email, name=name)
        return get_or_create_user_from_clerk(db, claims, via="webhook")

    try:
        user = await run_in_threadpool(_provision)
    except (UserProvisioningError, IntegrityError) as exc:
        # Colisión que el loop no puede resolver — típicamente un user.updated
        # que mueve el email a uno que ya pertenece a otra cuenta. Reintentar
        # 24 horas no lo va a arreglar.
        db.rollback()
        print(f"[provision] webhook FAILED clerk={clerk_id}: {exc!r}", flush=True)
        return {"ok": False, "reason": "conflicto de unicidad"}

    return {"ok": True, "user_id": user.id if user else None}


@app.get("/email/logo.png", include_in_schema=False)
def email_logo():
    """El wordmark de los mails, para las pantallas de desuscripción. Es el
    mismo PNG que viaja como adjunto CID en cada email (fondo #131324 y bordes
    redondeados propios, así que sobre el fondo de la página queda invisible
    el recorte)."""
    import lifecycle_emails
    from fastapi.responses import FileResponse

    if not lifecycle_emails.LOGO_PATH.exists():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(
        lifecycle_emails.LOGO_PATH,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _unsub_page(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<html><head><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex'></head>"
        "<body style='font-family:sans-serif;background:#131324;color:#f6f8fc;"
        f"text-align:center;padding:64px 24px;'>{body}</body></html>",
        status_code=status_code,
    )


_UNSUB_INVALID = "Ese link no es válido."
_UNSUB_DONE = (
    "<img src='/email/logo.png' width='163' height='61' alt='intervalo' style='display:block;margin:0 auto 24px;'>"
    "<p>Te desuscribiste de estos emails. No vas a recibir más.</p>"
)


@app.get("/email/unsubscribe", response_class=HTMLResponse)
def email_unsubscribe_page(token: str):
    """Pantalla de confirmación del link de baja del pie de los mails.

    El GET no escribe nada a propósito. Los escáneres de links de Gmail y
    Outlook visitan las URLs del cuerpo de un mail antes de que el usuario las
    toque, así que un GET que diera de baja desuscribiría gente sin que se
    entere y sin dejar rastro de que fue un bot. La baja real vive en el POST
    de abajo, que ningún prefetch dispara.

    El token se regenera desde el user_id ya verificado en vez de reflejar el
    de la query: así nada de lo que venga en la URL llega al HTML."""
    import lifecycle_emails

    user_id = lifecycle_emails.verify_unsubscribe_token(token)
    if user_id is None:
        return _unsub_page(_UNSUB_INVALID, status_code=400)

    safe_token = lifecycle_emails.unsubscribe_token(user_id)
    return _unsub_page(
        "<img src='/email/logo.png' width='163' height='61' alt='intervalo' style='display:block;margin:0 auto 24px;'>"
        "<p style='margin:0 0 24px;'>¿Querés dejar de recibir estos emails?</p>"
        f"<form method='post' action='/email/unsubscribe?token={safe_token}'>"
        "<button type='submit' style='background:#5457e5;color:#fff;border:0;"
        "font-size:14px;font-weight:700;padding:12px 28px;border-radius:8px;"
        "cursor:pointer;'>Desuscribirme</button>"
        "</form>"
    )


@app.post("/email/unsubscribe", response_class=HTMLResponse)
def email_unsubscribe_confirm(token: str, db: Session = Depends(get_db)):
    """Baja efectiva. La disparan dos cosas: el botón de la pantalla de arriba,
    y el POST que hacen Gmail y Yahoo por su propio botón de baja gracias al
    header `List-Unsubscribe-Post` (ver lifecycle_emails._send). Los dos casos
    son una acción deliberada de la persona, así que acá sí se escribe."""
    import lifecycle_emails

    user_id = lifecycle_emails.verify_unsubscribe_token(token)
    if user_id is None:
        return _unsub_page(_UNSUB_INVALID, status_code=400)

    user = db.query(User).filter(User.id == user_id).first()
    if user is not None:
        user.email_unsubscribed = True
        db.commit()

    return _unsub_page(_UNSUB_DONE)


# ── Correo entrante ───────────────────────────────────────────────────────────

@app.post("/webhooks/resend-inbound")
async def resend_inbound_webhook(request: Request):
    """Recibe el webhook email.received de Resend y reenvía el mail al buzón
    real (ver inbound_forward.py).

    Async y con el reenvío en threadpool: el SDK de Resend es bloqueante y una
    llamada lenta colgaría el event loop para toda la app. Ante un fallo se
    responde 500 a propósito — Resend reintenta con backoff y el mail nunca se
    pierde (queda guardado en Resend igual).
    """
    import json

    from inbound_forward import forward_received_email, verify_inbound_signature

    payload = (await request.body()).decode("utf-8")
    headers = {
        "svix-id": request.headers.get("svix-id"),
        "svix-timestamp": request.headers.get("svix-timestamp"),
        "svix-signature": request.headers.get("svix-signature"),
    }
    if not verify_inbound_signature(payload, headers):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event = json.loads(payload)
    # Cualquier otro evento suscripto por error se acusa como recibido y listo:
    # devolver error haría que Resend lo reintente para siempre.
    if event.get("type") != "email.received":
        return {"ok": True}

    email_id = (event.get("data") or {}).get("email_id")
    if not email_id:
        return {"ok": True}

    await run_in_threadpool(forward_received_email, email_id)
    return {"ok": True}


# ── Leaderboard ───────────────────────────────────────────────────────────────

# Filas a cada lado del usuario en la ventana centrada (`around_me`).
AROUND_WINDOW = 30

# Orden de cinturones para calcular el máximo del usuario. Una fila UnitState
# existe sólo cuando el cinturón está desbloqueado, así que el cinturón con
# mayor rank entre las filas del usuario es su máximo (en cualquier curso).
BELT_RANK = {"white": 0, "blue": 1, "violet": 2, "brown": 3}


def _max_belt_by_user(db: Session, user_ids: list[int]) -> dict[int, str]:
    """El cinturón más alto de cada usuario, en cualquier curso.

    Es lo que pinta el nombre en el ranking, así que lo usan las dos tablas que
    muestran personas: la individual y la de reclutas. Una sola función porque
    dos criterios distintos harían que la misma persona se viera de dos colores
    según por qué lista se la mire.

    Solo sobre los ids que se van a devolver, nunca sobre la tabla entera: antes
    esto agregaba `unit_states` completa en cada request del leaderboard.
    """
    if not user_ids:
        return {}
    out: dict[int, str] = {}
    for uid, belt in (
        db.query(UnitState.user_id, UnitState.belt)
        .filter(UnitState.user_id.in_(user_ids), UnitState.suspended.is_(False))
        .distinct()
        .all()
    ):
        if BELT_RANK.get(belt, -1) > BELT_RANK.get(out.get(uid, ""), -1):
            out[uid] = belt
    return out


def _mi_universidad(db: Session, user_id: int) -> str | None:
    """La universidad de una persona, con el MISMO criterio que todo lo demás.

    Delega en `xp_boost.enrollment_de_referencia` en vez de escribir un cuarto
    `order_by(enrolled_at)`: el repo ya tiene tres criterios distintos de "de qué
    universidad es esta persona" conviviendo (ver el docstring de esa función), y
    este es el que comparten el tag del ranking y el empuje de cafecito.
    """
    fila = xp_boost.enrollment_de_referencia(db, user_id)
    return fila.university if fila is not None else None


# Quién aparece en el ranking de Intervalo clásico.
#
# Gemelo de `game/router.py :: RESOLVIO_ACA`, y por el mismo motivo: la XP la
# puede subir un RECLUTA sin que el reclutador haya resuelto nunca un ejercicio
# acá (`referrals.acreditar_clasico` le suma a `total_xp` con un UPDATE crudo).
# Con el filtro viejo —`total_xp > 0`— alcanzaba con traer a alguien para entrar
# a la tabla, que es exactamente el agujero que el minijuego cerró en esta misma
# serie cambiando a `exercises_correct > 0`.
#
# La comparación es entre dos columnas de la misma fila y no un EXISTS contra
# `answers`: el ranking lo pregunta en cada request y `answers` es la tabla más
# grande del esquema.
#
# Los CINCO lugares que lo usan tienen que moverse JUNTOS. Si el que cuenta el
# total no filtra igual que el que arma la lista, los números de la cabecera
# dejan de cuadrar con las filas de abajo, y nadie reporta eso como bug.
VISIBLE_EN_RANKING = User.total_xp > User.referral_xp_earned


def _first_enrollment_subq():
    """Subquery user_id → (university, career, enrolled_at) del enrollment MÁS
    ANTIGUO de cada usuario (sus respuestas originales de onboarding), sin
    importar en qué curso — filtrar por course_id=1 perdía a usuarios enrolados
    únicamente en otro curso (ej. álgebra), que quedaban sin tag de universidad
    pese a tener los datos cargados.

    Antes esto se resolvía trayendo la tabla `enrollments` entera a memoria y
    quedándose con la primera fila de cada usuario. Como el leaderboard lo
    necesita en cada request (y `/public/university-leaderboard` es público),
    ahora la desambiguación la hace la BD con ROW_NUMBER y el resto de la
    agregación se apoya en este subquery.

    El desempate por `id` es explícito: dos enrollments con el mismo
    `enrolled_at` elegían una fila arbitraria según cómo ordenara el motor."""
    ranked = select(
        Enrollment.user_id,
        Enrollment.university,
        Enrollment.career,
        Enrollment.enrolled_at,
        func.row_number()
        .over(
            partition_by=Enrollment.user_id,
            order_by=(Enrollment.enrolled_at.asc(), Enrollment.id.asc()),
        )
        .label("rn"),
    ).subquery()
    return (
        select(
            ranked.c.user_id,
            ranked.c.university,
            ranked.c.career,
            ranked.c.enrolled_at,
        )
        .where(ranked.c.rn == 1)
        .subquery()
    )


def _career_bucket_sql(column):
    """Bucket de carrera: la carrera si es conocida (E/S/T/M), o 'Otra' —
    incluye el caso sin enrollment, donde la columna viene NULL."""
    return case((column.in_(_KNOWN_CAREERS), column), else_="Otra")


def _scope_filters(enroll, university: str | None, career: str | None) -> list:
    """Filtros de scope del leaderboard: universidad y/o bucket de carrera."""
    filters = []
    if university is not None:
        filters.append(enroll.c.university == university)
    if career is not None:
        filters.append(_career_bucket_sql(enroll.c.career) == career)
    return filters


@app.get("/leaderboard", response_model=LeaderboardResponse)
def get_leaderboard(
    university: str | None = Query(default=None),
    career: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    around_me: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Leaderboard ranked by total XP descending, paginado (offset/limit).

    El ranking, los totales y los datos del usuario actual se calculan sobre el
    set completo del scope (global o filtrado por universidad); solo `entries`
    devuelve la página pedida para el scroll infinito.

    Con `around_me=true` se ignoran `offset`/`limit` y se devuelve una ventana
    centrada en el usuario actual (`AROUND_WINDOW` filas a cada lado), para que
    el front cargue el ranking con el usuario en el medio y scrollee hacia ambos
    lados. Cada entry trae su `rank` absoluto, así el front conoce los bordes de
    la ventana y pide más arriba/abajo por offset.

    Todo lo que antes se resolvía trayendo la tabla `users` entera a memoria
    (orden, scope, totales, rank y paginado) lo hace ahora la BD: los totales son
    agregados, y de las filas solo viaja la página pedida. El orden canónico es
    (total_xp desc, id asc) — el desempate por id lo hace determinístico y es el
    mismo que usa `push_store._current_rank`.
    """
    enroll = _first_enrollment_subq()
    filters = _scope_filters(enroll, university, career)
    # Solo aparecen los que ya sumaron XP; las cuentas que nunca arrancaron solo
    # inflaban la cola. El propio usuario entra igual con 0: verse último dice
    # "estás acá, empezá a subir" — no aparecer diría "no existís". Con 0 XP su
    # rank queda count(>0)+1 y los demás ceros no compiten el desempate.
    filters = [*filters, sa_or(VISIBLE_EN_RANKING, User.id == current_user.id)]

    # Universidades presentes (set completo, sin aplicar el scope), para poblar
    # el filtro del front.
    universities = [
        u
        for (u,) in db.query(enroll.c.university)
        .join(User, User.id == enroll.c.user_id)
        .filter(enroll.c.university.isnot(None))
        .distinct()
        .order_by(enroll.c.university.asc())
        .all()
    ]

    # Totales del scope: una sola fila agregada, sin materializar usuarios.
    total_count, total_xp = (
        db.query(func.count(User.id), func.coalesce(func.sum(User.total_xp), 0))
        .outerjoin(enroll, enroll.c.user_id == User.id)
        .filter(*filters)
        .one()
    )
    total_exercises = (
        db.query(func.count(Answer.id))
        .join(User, User.id == Answer.user_id)
        .outerjoin(enroll, enroll.c.user_id == User.id)
        .filter(*filters)
        .scalar()
        or 0
    )

    # Posición del usuario actual dentro del scope: cuántos lo preceden en el
    # orden canónico. None si no entra en el scope (no aparece en el ranking).
    in_scope = (
        db.query(User.id)
        .outerjoin(enroll, enroll.c.user_id == User.id)
        .filter(User.id == current_user.id, *filters)
        .first()
        is not None
    )
    me = LeaderboardMe(total_xp=current_user.total_xp)
    my_index = None
    if in_scope:
        ahead = (
            db.query(func.count(User.id))
            .outerjoin(enroll, enroll.c.user_id == User.id)
            .filter(
                *filters,
                sa_or(
                    User.total_xp > current_user.total_xp,
                    sa_and(
                        User.total_xp == current_user.total_xp,
                        User.id < current_user.id,
                    ),
                ),
            )
            .scalar()
            or 0
        )
        my_index = int(ahead)
        me.rank = my_index + 1

    # Ventana de la página. En modo `around_me` se centra en el usuario actual.
    if around_me:
        if my_index is None:
            page_offset = 0
            page_size = limit
        else:
            page_offset = max(0, my_index - AROUND_WINDOW)
            page_size = (my_index + AROUND_WINDOW + 1) - page_offset
    else:
        page_offset = offset
        page_size = limit

    page = (
        db.query(User, enroll.c.university, enroll.c.career)
        .outerjoin(enroll, enroll.c.user_id == User.id)
        .filter(*filters)
        .order_by(User.total_xp.desc(), User.id.asc())
        .offset(page_offset)
        .limit(page_size)
        .all()
    )
    page_ids = [row[0].id for row in page]

    # Ejercicios y cinturón máximo SOLO de las filas que se devuelven (antes se
    # agregaba la tabla `answers` completa y se escaneaban todas las unit_states
    # en cada request).
    exercises_by_user = (
        dict(
            db.query(Answer.user_id, func.count(Answer.id))
            .filter(Answer.user_id.in_(page_ids))
            .group_by(Answer.user_id)
            .all()
        )
        if page_ids
        else {}
    )
    max_belt_by_user = _max_belt_by_user(db, page_ids)

    entries = [
        LeaderboardEntry(
            rank=page_offset + index + 1,
            user_id=user.id,
            name=user.username or user.display_name or user.name,
            username=user.username,
            total_xp=user.total_xp,
            exercises=int(exercises_by_user.get(user.id, 0)),
            is_current_user=user.id == current_user.id,
            career=row_career,
            university=row_university,
            emoji=emoji_tree.emoji_for(user.emoji_worn),
            belt=max_belt_by_user.get(user.id, "white"),
        )
        for index, (user, row_university, row_career) in enumerate(page)
    ]
    return LeaderboardResponse(
        entries=entries,
        total_xp=total_xp,
        total_exercises=total_exercises,
        total_count=total_count,
        has_more=page_offset + len(page) < total_count,
        me=me,
        universities=universities,
    )


# Carreras conocidas; cualquier otro valor (o null) cae en "Otra".
CAREER_BUCKETS = ["E", "S", "T", "M", "Otra"]
_KNOWN_CAREERS = [c for c in CAREER_BUCKETS if c != "Otra"]


@app.get("/leaderboard/universities", response_model=UniversityLeaderboardResponse)
def get_university_leaderboard(
    career: str | None = Query(default=None),
    university: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ranking de universidades: agrega estudiantes por universidad y carrera.

    A diferencia del leaderboard individual (paginado, ventana alrededor del
    usuario), acá se recorre el set completo una vez y se agregan los totales
    por universidad, así el front puede comparar universidades entre sí.

    `career` (bucket E/S/T/M/Otra): agrega contando solo estudiantes de esa
    carrera. `university`: limita a esa universidad (aislarla).

    La agregación la hace la BD (GROUP BY universidad × carrera): son a lo sumo
    unas decenas de filas, contra la tabla `users` completa en memoria que traía
    la versión anterior.
    """
    enroll = _first_enrollment_subq()
    bucket = _career_bucket_sql(enroll.c.career)

    filters = [
        enroll.c.university.isnot(None),
        enroll.c.university != "",
        # Mismo criterio que el leaderboard individual: los que nunca
        # resolvieron nada no cuentan como estudiantes de su universidad.
        VISIBLE_EN_RANKING,
    ]
    if university is not None:
        filters.append(enroll.c.university == university)
    if career is not None:
        filters.append(bucket == career)

    grouped = (
        db.query(
            enroll.c.university,
            bucket.label("bucket"),
            func.count(User.id),
            func.coalesce(func.sum(User.total_xp), 0),
            func.min(User.id),
        )
        .join(User, User.id == enroll.c.user_id)
        .filter(*filters)
        .group_by(enroll.c.university, bucket)
        .all()
    )

    # Agregación por universidad + agregado global por carrera. El orden de
    # inserción sigue el id del primer estudiante de cada universidad, que es el
    # que tenía el recorrido secuencial de `users` (y el que define los empates
    # del sort estable de abajo).
    by_uni: dict[str, dict] = {}
    career_totals = {c: 0 for c in CAREER_BUCKETS}
    total_students = 0
    for uni, b, students, xp, _first_id in sorted(grouped, key=lambda r: r[4]):
        total_students += students
        career_totals[b] += students
        agg = by_uni.setdefault(
            uni,
            {"total_xp": 0, "students": 0, "careers": {c: 0 for c in CAREER_BUCKETS}},
        )
        agg["total_xp"] += int(xp)
        agg["students"] += students
        agg["careers"][b] += students

    rows = [
        UniversityRankRow(
            university=uni,
            total_xp=agg["total_xp"],
            students=agg["students"],
            careers=agg["careers"],
        )
        for uni, agg in by_uni.items()
    ]
    # El ranking de universidades es por XP acumulado (estudiantes como
    # desempate secundario).
    rows.sort(key=lambda r: (r.total_xp, r.students), reverse=True)

    return UniversityLeaderboardResponse(
        rows=rows,
        total_students=total_students,
        total_universities=len(by_uni),
        career_totals=career_totals,
    )


# Cuántos reclutas tiene esta persona en Intervalo, y cuánto le aportaron.
#
# Espejo del `/leaderboard/recruits` del minijuego, con la misma asimetría a
# propósito: la LISTA muestra solo a los que ya aportaron algo, pero los
# CONTADORES cuentan a todos. "Trajiste a 8 personas" es la noticia aunque 3 no
# hayan arrancado, y una lista con tres renglones en cero se lee como un reproche.
_MAX_RECLUTAS = 50


@app.get("/leaderboard/recruits", response_model=RecruitsResponse)
def get_recruits(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    import referrals as referrals_mod

    propio = db.query(GamePlayer.id).filter(GamePlayer.user_id == current_user.id).first()
    if propio is None:
        # Sin jugador no hay a quién apuntarle un `?r=`: la arista de los dos
        # productos cuelga de `game_players.id`. No es un error, es que todavía
        # no compartió nada.
        return RecruitsResponse(
            entries=[],
            total_recruits=0,
            total_xp_given=0,
            share_percent=referrals_mod.SHARE_PERCENT,
            handle=current_user.username,
        )

    enroll = _first_enrollment_subq()
    filas = (
        db.query(User, enroll.c.university, enroll.c.career)
        .outerjoin(enroll, enroll.c.user_id == User.id)
        .filter(User.referred_by_player_id == propio.id, User.referral_xp_given > 0)
        .order_by(User.referral_xp_given.desc(), User.id.asc())
        .limit(_MAX_RECLUTAS)
        .all()
    )
    total_recruits, total_xp_given = (
        db.query(func.count(User.id), func.coalesce(func.sum(User.referral_xp_given), 0))
        .filter(User.referred_by_player_id == propio.id)
        .one()
    )
    belts = _max_belt_by_user(db, [u.id for (u, _uni, _car) in filas])
    return RecruitsResponse(
        entries=[
            RecruitEntry(
                rank=i + 1,
                username=u.username,
                university=uni,
                career=car,
                xp_given=u.referral_xp_given,
                belt=belts.get(u.id, "white"),
            )
            for i, (u, uni, car) in enumerate(filas)
        ],
        total_recruits=int(total_recruits or 0),
        total_xp_given=int(total_xp_given or 0),
        share_percent=referrals_mod.SHARE_PERCENT,
        handle=current_user.username,
    )


@app.get("/leaderboard/summary", response_model=LeaderboardSummaryResponse)
def get_leaderboard_summary(
    university: str | None = Query(default=None),
    career: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Números de la cabecera del leaderboard: estudiantes registrados,
    ejercicios completados y universidades presentes.

    `universities` siempre lista el set completo (para poblar el filtro), pero
    `total_students`/`total_exercises` respetan `career`/`university` si se
    pasan, igual que el scope de `/leaderboard`.

    `total_students` cuenta solo usuarios con XP positivo: el ranking ya no
    muestra a los que nunca arrancaron, y el contador tiene que hablar de la
    misma gente que la lista de abajo."""
    enroll = _first_enrollment_subq()
    has_university = sa_and(
        enroll.c.university.isnot(None), enroll.c.university != ""
    )

    universities = [
        u
        for (u,) in db.query(enroll.c.university)
        .filter(has_university)
        .distinct()
        .order_by(enroll.c.university.asc())
        .all()
    ]

    if university is None and career is None:
        total_students = (
            db.query(func.count())
            .select_from(enroll)
            .join(User, User.id == enroll.c.user_id)
            .filter(has_university, VISIBLE_EN_RANKING)
            .scalar()
            or 0
        )
        total_exercises = db.query(func.count(Answer.id)).scalar() or 0
    else:
        scoped = [has_university]
        if university is not None:
            scoped.append(enroll.c.university == university)
        if career is not None:
            scoped.append(_career_bucket_sql(enroll.c.career) == career)
        total_students = (
            db.query(func.count())
            .select_from(enroll)
            .join(User, User.id == enroll.c.user_id)
            .filter(*scoped, VISIBLE_EN_RANKING)
            .scalar()
            or 0
        )
        total_exercises = (
            db.query(func.count(Answer.id))
            .join(enroll, enroll.c.user_id == Answer.user_id)
            .filter(*scoped)
            .scalar()
            or 0
        )
    return LeaderboardSummaryResponse(
        total_students=total_students,
        total_exercises=total_exercises,
        universities=universities,
        # Sin filtrar por el scope de arriba, a propósito: ver el comentario del
        # campo en LeaderboardSummaryResponse. `active_boosts` arranca con
        # `hay_empujes`, que memoriza unos segundos el "no hay ninguno" —que es
        # el caso casi siempre— así que esto no le agrega consultas al resumen.
        boosts=[
            BoostTramo(
                university=b.university,
                multiplier=b.multiplier,
                cafecitos=b.cafecitos,
                donor_name=b.donor_name,
                expires_in_seconds=b.expires_in_seconds,
                aforo=b.aforo,
            )
            for b in game_boosts.active_boosts(db)
        ],
        university=_mi_universidad(db, current_user.id),
    )


@app.get("/public/university-leaderboard", response_model=PublicUniversityLeaderboardResponse)
def get_public_university_leaderboard(db: Session = Depends(get_db)):
    """Snapshot agregado sin auth de las universidades top (por XP, mismo
    orden que /leaderboard/universities), para la landing (marketing-home.tsx)
    — un visitante sin cuenta no tiene sesión para pegarle a ese endpoint. Sin
    PII: solo universidad + conteos, los mismos números que ya ve cualquier
    usuario logueado en el leaderboard.

    Es el único endpoint del leaderboard sin auth, así que es el que más importa
    que no traiga `users` + `enrollments` enteras a memoria en cada visita a la
    landing: un GROUP BY devuelve una fila por universidad. Solo cuentan los
    usuarios con XP positivo, igual que en el leaderboard de la app — los dos
    números tienen que hablar de la misma gente."""
    enroll = _first_enrollment_subq()

    # El orden de inserción sigue el enrollment más antiguo de cada universidad,
    # que es el que define los empates del sort estable por XP de abajo.
    grouped = (
        db.query(
            enroll.c.university,
            func.count().label("students"),
            func.coalesce(func.sum(User.total_xp), 0).label("total_xp"),
        )
        .join(User, User.id == enroll.c.user_id)
        .filter(
            enroll.c.university.isnot(None),
            enroll.c.university != "",
            VISIBLE_EN_RANKING,
        )
        .group_by(enroll.c.university)
        .order_by(func.min(enroll.c.enrolled_at).asc(), enroll.c.university.asc())
        .all()
    )

    rows = [
        PublicUniversityStat(university=u, students=students, total_xp=int(total_xp))
        for u, students, total_xp in grouped
    ]
    rows.sort(key=lambda r: r.total_xp, reverse=True)
    return PublicUniversityLeaderboardResponse(rows=rows[:8])


# ── Emoji unlock tree (badges) ──────────────────────────────────────────────────

def _emoji_bucket(db: Session, user: User) -> str | None:
    """Bucket de carrera del usuario (E/S/T/M/Otra), de su enrollment de
    referencia — el más antiguo sin importar el curso.

    Sale de `xp_boost.enrollment_de_referencia`, que es la misma función que usa
    el empuje. Acá había una tercera copia del criterio, y le faltaba el
    desempate por `id`: dos enrollments con el mismo `enrolled_at` elegían una
    fila arbitraria según cómo ordenara el motor, así que el árbol de emojis
    podía cambiar de rama entre dos requests sin que nadie tocara nada."""
    fila = xp_boost.enrollment_de_referencia(db, user.id)
    return fila.career if fila else None


def _emoji_state(db: Session, user: User) -> EmojiStateResponse:
    return EmojiStateResponse(
        bucket=_emoji_bucket(db, user),
        total_xp=user.total_xp,
        worn=user.emoji_worn,
    )


@app.get("/user/emoji-tree", response_model=EmojiStateResponse)
def get_emoji_state(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Estado del árbol de desbloqueo del usuario (bucket, XP, vestido). El
    conjunto desbloqueado se deriva client-side de total_xp — no hay elección
    ni endpoint de unlock, todo lo de la profundidad alcanzada se desbloquea
    solo."""
    return _emoji_state(db, current_user)


class EmojiWornRequest(BaseModel):
    node_id: str | None = None


@app.put("/user/emoji/worn", response_model=EmojiStateResponse)
def set_worn_emoji(
    body: EmojiWornRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Viste un emoji ya desbloqueado (profundidad alcanzada por XP). None =
    raíz del bucket (default)."""
    bucket = _emoji_bucket(db, current_user)
    ok, reason = emoji_tree.can_wear(body.node_id, bucket, current_user.total_xp)
    if not ok:
        raise HTTPException(status_code=422, detail=reason)

    current_user.emoji_worn = emoji_tree.normalize_worn(body.node_id, bucket)
    db.commit()
    db.refresh(current_user)
    return _emoji_state(db, current_user)


# ── Session endpoints ─────────────────────────────────────────────────────────

@app.post("/session/start", response_model=SessionStartResponse)
def start_session(
    body: StartSessionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Start a new session linked to authenticated user in database."""
    from session_store import create_session_db, DailySessionLimitError

    course_id = _resolve_course_id(body.course, db)

    try:
        result = create_session_db(current_user.id, course_id, db)
    except DailySessionLimitError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return result


@app.post("/session/start-practice")
def start_practice_session(
    body: StartPracticeSessionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Start a Practice session: random exercises from selected (belt, topic) items, no SM-2 logic."""
    from session_store import create_practice_session_db

    if not body.items:
        raise HTTPException(status_code=400, detail="Seleccioná al menos un tema.")
    if body.count < 1:
        raise HTTPException(status_code=400, detail="El número de ejercicios debe ser al menos 1.")
    course_id = _resolve_course_id(body.course, db)
    try:
        return create_practice_session_db(
            user_id=current_user.id, course_id=course_id,
            items=[i.model_dump() for i in body.items], count=body.count, db=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/session/start-test")
def start_test_session(
    body: StartTestSessionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Start a QA/test session: ALL exercises in each selected item, no SR logic."""
    from session_store import create_test_session_db

    if not body.items:
        raise HTTPException(status_code=400, detail="Seleccioná al menos un item.")
    course_id = _resolve_course_id(body.course, db)
    try:
        return create_test_session_db(
            user_id=current_user.id, course_id=course_id,
            items=[i.model_dump() for i in body.items], db=db,
            shuffle=body.shuffle,
            filters=body.filters.model_dump() if body.filters else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/session/answer")
def submit_answer(
    body: AnswerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Submit answer for exercise - validates ownership and saves to DB."""
    from session_store import SessionClosedError, record_answer_db

    try:
        # Parse session_id as integer (it's stored as string in frontend but is DB ID)
        session_id_db = int(body.session_id)

        result = record_answer_db(
            session_id_db,
            current_user.id,
            body.exercise_id,
            body.answer_index,
            body.attempts,
            body.response_time_s,
            db,
            exercise_external_id=body.exercise_external_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SessionClosedError as exc:
        # Replay del runner sobre una sesión ya cerrada (ver record_answer_db).
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session_id format")

    return result


# Holgado para una sesión de QA (50 ejercicios con feedback largo entran de
# sobra) y chico como para que un request no escriba nada raro en disco.
DEV_FEEDBACK_MAX_CHARS = 500_000


@app.post("/dev/test-feedback", dependencies=[Depends(require_dev_endpoints)])
def save_test_feedback(body: TestFeedbackSaveRequest):
    """QA-only: vuelca a disco el feedback de una pasada de test mode, sobre-
    escribiendo el mismo archivo en cada guardado (debounce del lado del
    cliente). Existe porque localStorage por sí solo no alcanza: si el tab
    pierde el sessionStorage de la sesión (se cierra, se refresca), la UI
    muestra "sesión expirada" y no hay forma de volver a entrar al runner
    para descargar el feedback ya tipeado."""
    # El nombre sale de un regex que solo deja [A-Za-z0-9_-], así que no hay
    # traversal posible; el tope de tamaño acota lo único que quedaba sin
    # límite, que es cuánto se escribe por request.
    if len(body.doc) > DEV_FEEDBACK_MAX_CHARS:
        raise HTTPException(status_code=413, detail="doc too large")
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", body.session_id)
    out_dir = os.path.join(os.path.dirname(__file__), ".test-feedback")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{safe_id}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body.doc)
    return {"ok": True, "path": path}


FEEDBACK_XP = 1  # XP fijo por responder una encuesta o enviar un reporte (no por la impression).


def _sumar_xp(db: Session, user: User, cuanto: int) -> None:
    """Suma XP con un UPDATE RELATIVO, no leyendo y escribiendo en Python.

    `current_user` se cargó al empezar el request y la encuesta aparece DURANTE
    la sesión, así que entre esa lectura y este commit puede haber entrado una
    respuesta con su propio pago. Un `SET total_xp = <lo que leí> + 1` borra esa
    respuesta entera.

    Es el mismo motivo, y el mismo remedio, que `session_store.record_answer_db`
    —cuyo comentario nombra a este lugar como el escritor concurrente del que hay
    que cuidarse— y que el pago al reclutador en `referrals.py`.
    """
    db.query(User).filter(User.id == user.id).update(
        {User.total_xp: User.total_xp + cuanto}, synchronize_session=False
    )
    # El objeto en memoria quedó viejo por el `synchronize_session=False`: se
    # expira para que quien lo lea después (la respuesta, un refresh) traiga el
    # valor de la base y no el de antes del UPDATE.
    db.expire(user, ["total_xp"])


@app.post("/session/feedback", response_model=SessionFeedbackResponse)
def submit_session_feedback(
    body: SessionFeedbackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Micro-encuesta post-ejercicio (dificultad/explicación/interés) y reporte
    de problemas de contenido. Flujo en dos pasos para no perder la impression si
    el usuario cierra/navega antes de responder (ver feedback_survey.py):
    "impression" crea la fila (answered_at=None), "answer" la completa. "report"
    (canal C, siempre disponible) crea la fila ya resuelta en un solo paso."""
    from datetime import datetime

    from feedback_survey import SURVEY_TYPES, validate_reason
    from models import Exercise, ExerciseFeedback, Session as SessionModel

    try:
        session_id_db = int(body.session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session_id format")

    session_row = (
        db.query(SessionModel)
        .filter(SessionModel.id == session_id_db, SessionModel.user_id == current_user.id)
        .first()
    )
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if body.action == "impression":
        if not body.exercise_external_id or body.question_type not in SURVEY_TYPES:
            raise HTTPException(status_code=422, detail="Missing exercise_external_id/question_type")
        exists = (
            db.query(Exercise.id)
            .filter(Exercise.course_id == session_row.course_id, Exercise.external_id == body.exercise_external_id)
            .first()
        )
        if exists is None:
            raise HTTPException(status_code=404, detail="Exercise not found")
        entry = ExerciseFeedback(
            user_id=current_user.id,
            session_id=session_row.id,
            course_id=session_row.course_id,
            exercise_external_id=body.exercise_external_id,
            question_type=body.question_type,
        )
        db.add(entry)
        db.commit()
        return {"success": True, "feedback_id": entry.id}

    if body.action == "answer":
        if body.feedback_id is None:
            raise HTTPException(status_code=422, detail="Missing feedback_id")
        entry = (
            db.query(ExerciseFeedback)
            .filter(ExerciseFeedback.id == body.feedback_id, ExerciseFeedback.user_id == current_user.id)
            .first()
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="Feedback impression not found")
        entry.value = body.value
        entry.free_text = body.free_text
        entry.reason = validate_reason(entry.question_type, body.value, body.reason)
        entry.answered_at = datetime.utcnow()
        _sumar_xp(db, current_user, FEEDBACK_XP)
        db.commit()
        return {"success": True, "feedback_id": entry.id, "xp_earned": FEEDBACK_XP}

    if body.action == "report":
        if not body.exercise_external_id:
            raise HTTPException(status_code=422, detail="Missing exercise_external_id")
        exists = (
            db.query(Exercise.id)
            .filter(Exercise.course_id == session_row.course_id, Exercise.external_id == body.exercise_external_id)
            .first()
        )
        if exists is None:
            raise HTTPException(status_code=404, detail="Exercise not found")
        now = datetime.utcnow()
        entry = ExerciseFeedback(
            user_id=current_user.id,
            session_id=session_row.id,
            course_id=session_row.course_id,
            exercise_external_id=body.exercise_external_id,
            question_type="C",
            value=body.value,
            free_text=body.free_text,
            shown_at=now,
            answered_at=now,
        )
        db.add(entry)
        _sumar_xp(db, current_user, FEEDBACK_XP)
        db.commit()
        return {"success": True, "feedback_id": entry.id, "xp_earned": FEEDBACK_XP}

    raise HTTPException(status_code=422, detail="Invalid action")


@app.get("/session/{session_id}/summary", response_model=SessionSummaryResponse)
def session_summary(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get session summary from database - validates ownership."""
    from session_store import get_summary_db

    try:
        session_id_db = int(session_id)
        return get_summary_db(session_id_db, current_user.id, db)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session_id format")


# ── Feedback ──────────────────────────────────────────────────────────────────

def _send_feedback_email(categoria: str, user_id: int, mensaje: str) -> None:
    """Notify via Resend. Best-effort: logs and swallows any failure so the
    user's request never blocks on the email provider."""
    import logging

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logging.warning("RESEND_API_KEY not set — skipping feedback email")
        return

    to_email = os.environ.get("FEEDBACK_TO_EMAIL", "nvrancovich@gmail.com")
    from_email = os.environ.get("FEEDBACK_FROM_EMAIL", "onboarding@resend.dev")

    try:
        import resend

        resend.api_key = api_key
        resend.Emails.send({
            "from": from_email,
            "to": to_email,
            "subject": f"[{categoria}] Feedback de usuario {user_id}",
            "text": f"Categoría: {categoria}\nUsuario: {user_id}\n\n{mensaje}",
        })
    except Exception:
        logging.exception("Failed to send feedback email via Resend")


@app.post("/feedback", response_model=SimpleResponse)
def submit_feedback(
    body: FeedbackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save user feedback and notify via email. user_id comes from the token.

    The email is best-effort — if Resend fails we still return success so the
    user isn't blocked by a provider outage.
    """
    from models import Feedback

    mensaje = body.mensaje.strip()
    if not mensaje:
        raise HTTPException(status_code=422, detail="El mensaje no puede estar vacío.")

    entry = Feedback(
        user_id=current_user.id,
        categoria=body.categoria,
        mensaje=mensaje,
    )
    db.add(entry)
    db.commit()

    _send_feedback_email(body.categoria, current_user.id, mensaje)

    return {"success": True}


# ── Panel de métricas ─────────────────────────────────────────────────────────
#
# Un panel de solo lectura detrás de un link secreto, montado acá y no en un
# servicio aparte: la sesión de base, los modelos y el pipeline de deploy ya
# existen, y los datos son lo bastante chicos como para que agregarlos no
# justifique una infraestructura propia (ver backend/metrics/queries.py).
#
# La ruta es `def` (no `async def`), así que FastAPI la corre en el threadpool:
# una consulta lenta del panel no bloquea el event loop de la API.

# El panel hace un puñado de SELECT completos por carga. Un F5 repetido no tiene
# por qué pegarle a la base cada vez, y los números no cambian de un segundo al
# otro. El backend corre con una réplica, así que un dict en proceso alcanza.
_PANEL_TTL_SECONDS = 120
_panel_cache: dict[str, tuple[float, dict]] = {}


def _panel_payload(week, db: Session) -> dict:
    from metrics import queries

    key = week.isoformat()
    hit = _panel_cache.get(key)
    if hit and time.time() - hit[0] < _PANEL_TTL_SECONDS:
        return hit[1]
    payload = queries.build(db, week)
    _panel_cache.clear()  # una sola semana en memoria; el panel se mira de a una
    _panel_cache[key] = (time.time(), payload)
    return payload


def _panel_week(w: str | None):
    """`?w=YYYY-MM-DD` → el lunes de esa semana. Sin parámetro, la semana en
    curso. Una fecha inválida cae a la semana actual en vez de tirar 422: es un
    panel, no una API, y un link mal pegado tiene que mostrar algo.

    Se acota con `clamp_week` para que un `?w=` viejo escrito a mano no abra
    semanas anteriores a la difusión, que el selector ya no ofrece."""
    from datetime import date as _date

    from metrics.queries import clamp_week, week_start
    from metrics.render import week_of_today

    if w:
        try:
            return clamp_week(week_start(_date.fromisoformat(w)))
        except ValueError:
            pass
    return week_of_today()


def _require_panel_token(token: str) -> None:
    """404 y no 403, por el mismo motivo que `require_dev_endpoints`: una ruta
    que no existe no le confirma a nadie que exista en otro lado. Vale también
    cuando la variable no está configurada — un entorno nuevo no expone el panel
    por olvidarse de setearla."""
    expected = os.environ.get("DASHBOARD_TOKEN")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=404, detail="Not Found")


_PANEL_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Robots-Tag": "noindex, nofollow",
}


@app.get("/panel/{token}", response_class=HTMLResponse, include_in_schema=False)
def panel_page(token: str, w: str | None = None, db: Session = Depends(get_db)):
    from metrics.render import page

    _require_panel_token(token)
    week = _panel_week(w)
    return HTMLResponse(
        page(_panel_payload(week, db), token=token),
        headers=_PANEL_HEADERS,
    )


# El panel del juego es una página aparte y no una sección más del de arriba:
# comparte el token y las primitivas de gráfico, pero no el vocabulario (acá una
# «sesión» se reconstruye por huecos y el P1 excluye los ejercicios mirados con
# la tabla abierta). Mezclarlos haría que la misma palabra signifique dos cosas
# en la misma pantalla, que es exactamente cómo se arruina un panel.
_game_panel_cache: dict[str, tuple[float, dict]] = {}


def _game_panel_payload(week, db: Session, corte: str = "total") -> dict:
    from metrics import game_queries

    # La clave lleva el corte: el desglose cambia el payload, y sin esto pasar
    # de «por universidad» a «por aparato» devolvía el gráfico anterior durante
    # dos minutos.
    key = f"{week.isoformat()}:{corte}"
    hit = _game_panel_cache.get(key)
    if hit and time.time() - hit[0] < _PANEL_TTL_SECONDS:
        return hit[1]
    payload = game_queries.build(db, week, corte=corte)
    # Se guardan los últimos cuatro y no uno solo: los cuatro cortes son links
    # de la misma barra y se recorren de a uno, así que con una sola ranura cada
    # click volvía a recorrer todas las tablas.
    while len(_game_panel_cache) >= 4:
        _game_panel_cache.pop(next(iter(_game_panel_cache)))
    _game_panel_cache[key] = (time.time(), payload)
    return payload


def _game_panel_week(w: str | None):
    """`?w=YYYY-MM-DD` → el lunes de esa semana. Sin parámetro, la semana en
    curso. Una fecha inválida cae a la semana actual en vez de tirar 422: es un
    panel, no una API.

    Se acota con `clamp_week` por los dos lados. Por arriba porque una semana
    futura solo puede dar ceros y un cero se lee como caída; por abajo porque
    antes de la difusión el juego no tenía a nadie, y esas semanas vacías no son
    una caída sino la ausencia de producto (ver game_queries.FIRST_WEEK)."""
    from datetime import date as _date

    from metrics.game_queries import clamp_week
    from metrics.game_render import week_of_today
    from metrics.queries import week_start

    if w:
        try:
            return clamp_week(week_start(_date.fromisoformat(w)))
        except ValueError:
            pass
    return clamp_week(week_of_today())


@app.get("/panel/{token}/dx", response_class=HTMLResponse, include_in_schema=False)
def game_panel_page(token: str, w: str | None = None, s: str = "titulares",
                    corte: str = "total", db: Session = Depends(get_db)):
    from metrics.game_render import page as game_page

    _require_panel_token(token)
    week = _game_panel_week(w)
    return HTMLResponse(
        game_page(_game_panel_payload(week, db, corte), token=token, seccion=s),
        headers=_PANEL_HEADERS,
    )


@app.get("/panel/{token}/dx/data.json", include_in_schema=False)
def game_panel_data(token: str, w: str | None = None, corte: str = "total",
                    db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse

    _require_panel_token(token)
    payload = _game_panel_payload(_game_panel_week(w), db, corte)
    # `default=str` porque varios bloques llevan `date`/`datetime` adentro (la
    # semana de cada fila, el inicio de cada empuje). Serializarlos a ISO es más
    # útil que aplanarlos en las consultas: el JSON existe para poder hacer
    # cuentas afuera, y ahí una fecha en texto ISO se vuelve a parsear sola.
    return JSONResponse(
        json.loads(json.dumps(payload, default=str)), headers=_PANEL_HEADERS)


@app.get("/panel/{token}/derivemos{resto:path}", include_in_schema=False)
def game_panel_legacy(token: str, resto: str = "", w: str | None = None):
    """La ruta vieja del panel del juego, que ahora vive en `/dx`.

    Existe por una razón práctica: el link lleva un token de 32 caracteres y no
    se retipea, así que un 404 en el bookmark viejo obliga a ir a buscarlo a
    Railway. Se puede borrar cuando ese bookmark ya no le importe a nadie.

    Valida el token igual que el panel: una ruta que redirige sin mirar diría
    que el panel existe, que es justo lo que `_require_panel_token` evita.
    """
    _require_panel_token(token)
    destino = f"/panel/{token}/dx{resto}"
    return RedirectResponse(destino + (f"?w={w}" if w else ""), status_code=308)


@app.get("/panel/{token}/data.json", include_in_schema=False)
def panel_data(token: str, w: str | None = None, db: Session = Depends(get_db)):
    """El mismo payload que la página, en JSON.

    Es lo que hace que el reporte del domingo salga de una sola fuente en vez de
    copiar doscientos números a mano desde cuatro sistemas, que es como se armó
    el de la semana del 22/08."""
    from fastapi.responses import JSONResponse

    _require_panel_token(token)
    return JSONResponse(_panel_payload(_panel_week(w), db), headers=_PANEL_HEADERS)
