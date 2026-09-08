"""
session_store.py — Session lifecycle: build, answer, summarize.

Unit-level spaced repetition: each (belt, topic, exercise_type) triple is one
tracked unit. A topic is considered "mastered" only once *every* one of its
exercise_types has graduated into the reviewing phase.
"""

from __future__ import annotations

import json
import random
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import NamedTuple
from datetime import datetime, date, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from algorithm import (
    Belt,
    SM2Config,
    SM2UnitState,
    TopicKey,
    UnitKey,
    DIFFICULTY_MIN_SAMPLES,
    DIFFICULTY_WINDOW,
    STREAK_RESET_AFTER_DAYS,
    elo_update,
    XP_STREAK_BONUS,
    XP_STREAK_INTERVAL,
    difficulty_multiplier,
    build_session,
    effective_multiplier,
    is_topic_mastered,
    load_belt_catalogs,
    practice_xp_split,
    quality_from_attempts,
    quality_from_time,
    review_xp_split,
    streak_info,
    streak_multiplier,
    update_unit_state,
    xp_from_boost,
)
import referrals
import summary_asks
import xp_boost
from exercise_bank import (
    _row_to_dict,
    course_exercise_types,
    get_exercise_db,
    list_exercises_db,
    mark_exercise_served,
)
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession
from models import (
    Answer,
    Course,
    CourseProgress,
    Exercise,
    ItemDifficulty,
    ItemExerciseCycle,
    Session as SessionModel,
    UnitState,
    UnitStateArchive,
    User,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# ── User timezone / "today" ───────────────────────────────────────────────────
# El "día de estudio" de la repetición espaciada se define en la zona horaria del
# usuario, NO en la del servidor (UTC en Railway). Sin esto, entre las ~21:00 y la
# medianoche de Argentina (UTC−3) date.today() ya es "mañana" y habilita repasos
# antes de tiempo. Fallback a Argentina mientras el usuario no tenga tz persistida.
DEFAULT_TZ = "America/Argentina/Buenos_Aires"


# ── Creación concurrente de filas ─────────────────────────────────────────────
# Varias filas de este módulo se crean "lazy": se busca, y si no está, se
# inserta. Entre el SELECT y el INSERT hay una ventana, y el frontend la abre de
# par en par — el dashboard pide /user/progress de los tres cursos en paralelo y
# el alta de onboarding siembra units mientras esas requests ya están en vuelo.
# Dos requests del mismo usuario llegaban al INSERT a la vez y el perdedor
# reventaba con UniqueViolation → 500 (visto en producción sobre
# `unique_user_course_progress`).
#
# La fila que ganó la carrera es igual de buena que la nuestra: estas funciones
# la descartan y siguen con la del otro. Nada que reintentar ni que reconciliar.

class _Conflicto:
    """Resultado de _tolerating_duplicates: guarda el choque, si lo hubo."""

    error: IntegrityError | None = None


@contextmanager
def _tolerating_duplicates(db: DBSession):
    """Inserta filas tolerando que otra transacción haya creado las mismas.

    Va sobre un SAVEPOINT y no sobre un rollback pelado a propósito: estas
    inserciones ocurren en medio de requests que ya traen cambios propios sin
    guardar (el update SM-2 de una respuesta, sin ir más lejos). Un rollback
    entero descartaría también ESO, y perder la respuesta del usuario en
    silencio es peor que el 500 que veníamos a arreglar. Con el savepoint se
    revierte únicamente lo de adentro.

    El flush previo es parte del contrato: asienta lo pendiente del caller
    ANTES de abrir el savepoint, para que quede afuera de lo que se revierte."""
    conflicto = _Conflicto()
    db.flush()
    try:
        with db.begin_nested():
            yield conflicto
    except IntegrityError as exc:
        # Savepoint ya revertido por el context manager. Nos ganaron de mano y
        # sus filas son las que queríamos: seguimos con esas. Se guarda el error
        # por si el caller descubre que el choque era por otra cosa.
        conflicto.error = exc


def _user_zone(db: DBSession, user_id: int) -> ZoneInfo:
    user = db.get(User, user_id)
    tz_name = (user.timezone if user else None) or DEFAULT_TZ
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TZ)


def user_today(db: DBSession, user_id: int) -> date:
    """Fecha 'hoy' en la zona horaria del usuario (fallback Argentina)."""
    return datetime.now(_user_zone(db, user_id)).date()


def _user_day_start_utc(db: DBSession, user_id: int) -> datetime:
    """Medianoche del día actual del usuario, expresada en UTC naive (para comparar
    contra columnas DateTime que guardan datetime.utcnow())."""
    tz = _user_zone(db, user_id)
    start_local = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz)
    return start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


# ── Course slug resolution ────────────────────────────────────────────────────

_COURSE_SLUG_CACHE: dict[int, str] = {}


def _get_course_slug(course_id: int, db: DBSession) -> str:
    slug = _COURSE_SLUG_CACHE.get(course_id)
    if slug is not None:
        return slug
    course = db.query(Course).filter(Course.id == course_id).first()
    if course is None:
        raise ValueError(f"Course id={course_id} no encontrado en BD")
    _COURSE_SLUG_CACHE[course_id] = course.slug
    return course.slug


def _all_topic_keys(course_id: int, db: DBSession) -> list[TopicKey]:
    """Full ordered list of topic keys across all belts in canonical order.

    El orden respeta course.json: cinturones en orden, y dentro de cada cinturón
    los temas aplanados unidad-por-unidad (ver BeltCatalog.topics). Esto hace que
    los temas de la unidad 1 se introduzcan antes que los de la unidad 2."""
    slug = _get_course_slug(course_id, db)
    catalogs = load_belt_catalogs(slug)
    keys: list[TopicKey] = []
    for belt in catalogs:  # dict en orden de course.json
        keys.extend(catalogs[belt].all_keys())
    return keys


def _user_catchup_types(
    user_id: int, course_id: int, db: DBSession,
) -> set[tuple[str, str, str]]:
    """(belt, topic, exercise_type) de las units marcadas catch-up del usuario."""
    rows = db.query(
        UnitState.belt, UnitState.topic, UnitState.exercise_type,
    ).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
        UnitState.is_catchup.is_(True),
    ).all()
    return {(r.belt, r.topic, r.exercise_type) for r in rows}


def _mastery_types(
    user_id: int,
    course_id: int,
    topic_key: TopicKey,
    db: DBSession,
    *,
    types: dict[tuple[str, str], list[str]] | None = None,
) -> list[str]:
    """exercise_types de un tema que cuentan para maestría: los del banco menos
    los que en este usuario existen como catch-up (repaso extra, no despromociona).

    `types` es el mapa (belt, topic)→exercise_types del curso; si el caller ya lo
    tiene cargado se reusa en vez de volver a pegarle a la BD."""
    catchup = _user_catchup_types(user_id, course_id, db)
    types = types if types is not None else course_exercise_types(course_id, db)
    return [
        et for et in types.get((topic_key.belt.value, topic_key.topic), [])
        if (topic_key.belt.value, topic_key.topic, et) not in catchup
    ]


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ExerciseInSession:
    exercise_id: str
    unit_key: UnitKey
    question: str
    options: list[str]
    correct_index: int
    feedback_correct: str
    feedback_incorrect: str | list
    has_math: bool = False
    graph_fn: str = ""
    graph_view: list | None = None
    graph_shade: list | None = None
    graph_free_aspect: bool = False
    table: dict | None = None
    explanation: str | None = None
    external_id: str = ""

    @property
    def topic_key(self) -> TopicKey:
        return self.unit_key.topic_key

    @property
    def exercise_type(self) -> str:
        return self.unit_key.exercise_type


@dataclass
class SessionState:
    session_id: str
    user_name: str
    unit_states: dict[UnitKey, SM2UnitState]
    exercises: list[ExerciseInSession]
    results: list[dict] = field(default_factory=list)
    xp_session: int = 0
    streak: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)


# Caché en memoria de la sesión en curso (evita rearmar el estado en cada
# respuesta). Es sólo caché: `_reconstruct_session_state` la recrea desde la BD
# si falta. Sin barrido crecía para siempre — una entrada por sesión desde el
# último deploy — así que se purga por antigüedad; una sesión de estudio real
# dura minutos, no un día.
_sessions: dict[str, SessionState] = {}
_SESSION_TTL = timedelta(hours=24)


def _sweep_sessions(now: datetime | None = None) -> None:
    """Descarta del caché las sesiones más viejas que `_SESSION_TTL`."""
    now = now or datetime.utcnow()
    stale = [
        sid for sid, state in _sessions.items()
        if now - state.created_at > _SESSION_TTL
    ]
    for sid in stale:
        _sessions.pop(sid, None)


class DailySessionLimitError(Exception):
    """Raised when the user already started/finished their main session today."""


class SessionClosedError(Exception):
    """Raised when an answer arrives for a session whose summary already ran
    (finished_at set). Un replay del runner (back del navegador, tab duplicado)
    no debe poder seguir sumando respuestas ni XP."""


def _has_main_session_today(user_id: int, course_id: int, db: DBSession) -> bool:
    today_start = _user_day_start_utc(db, user_id)
    return db.query(SessionModel).filter(
        SessionModel.user_id == user_id,
        SessionModel.course_id == course_id,
        SessionModel.mode == "main",
        SessionModel.started_at >= today_start,
    ).first() is not None


def _has_pending_items(user_id: int, course_id: int, db: DBSession) -> bool:
    """Whether the user has any review item due today or earlier."""
    today = user_today(db, user_id)
    return db.query(UnitState).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
        UnitState.next_due.isnot(None),
        UnitState.next_due <= today,
    ).first() is not None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _current_exercise_types(
    course_id: int,
    db: DBSession,
    *,
    types: dict[tuple[str, str], list[str]] | None = None,
) -> set[tuple[str, str, str]]:
    """(belt, topic, exercise_type) combos con contenido real hoy en `exercises`.

    Podar contenido (sacar un exercise_type de un topic, o un topic entero)
    dejaba huérfanas las `unit_states` de los usuarios que ya lo tenían activo:
    seguían contando como activas/pendientes y podían ser elegidas por
    `build_session`, que fallaba al no encontrar ejercicios para servir. Todo
    lo que consulta o cuenta unit_states para "qué está vigente hoy" filtra
    por este set, así un prune de contenido no requiere cirugía manual de
    datos por usuario."""
    types = types if types is not None else course_exercise_types(course_id, db)
    return {
        (belt, topic, et)
        for (belt, topic), ets in types.items()
        for et in ets
    }


def _topics_with_units(
    user_id: int,
    course_id: int,
    db: DBSession,
    *,
    current: set[tuple[str, str, str]],
) -> set[tuple[str, str]]:
    """(belt, topic) del usuario con al menos una unit vigente, en UNA query.

    Reemplaza el `_topic_has_any_units` por tema dentro de los loops que
    recorren el catálogo entero: la pregunta es la misma, pero contestada
    sobre las filas ya cargadas en memoria."""
    rows = db.query(
        UnitState.belt, UnitState.topic, UnitState.exercise_type,
    ).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
    ).all()
    return {
        (r.belt, r.topic)
        for r in rows
        if (r.belt, r.topic, r.exercise_type) in current
    }


def _topic_has_any_units(
    user_id: int,
    course_id: int,
    topic_key: TopicKey,
    db: DBSession,
) -> bool:
    """Versión de un solo tema (para callers que preguntan por uno, no por el
    catálogo entero — esos usan `_topics_with_units`)."""
    current = _current_exercise_types(course_id, db)
    rows = db.query(UnitState).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
        UnitState.belt == topic_key.belt.value,
        UnitState.topic == topic_key.topic,
    ).all()
    return any((r.belt, r.topic, r.exercise_type) in current for r in rows)


def _create_topic_units(
    user_id: int,
    course_id: int,
    topic_key: TopicKey,
    db: DBSession,
    *,
    is_catchup: bool = False,
    types: dict[tuple[str, str], list[str]] | None = None,
) -> list[str]:
    """Create UnitState rows for every exercise_type of the given topic."""
    types = types if types is not None else course_exercise_types(course_id, db)
    types_for_topic = types.get((topic_key.belt.value, topic_key.topic), [])
    today = user_today(db, user_id)
    for et in types_for_topic:
        db.add(UnitState(
            user_id=user_id,
            course_id=course_id,
            belt=topic_key.belt.value,
            topic=topic_key.topic,
            exercise_type=et,
            phase="learning",
            step_index=0,
            ease_factor=2.5,
            interval_days=1,
            repetitions=0,
            next_due=today,
            attempted=False,
            is_catchup=is_catchup,
        ))
    return types_for_topic


# Ítem del ejercicio de prueba del onboarding, por curso. Mapea uno a uno con el
# ejercicio que muestra el wizard en el front (ver ONBOARDING_EXERCISES): si allá
# se cambia el ejercicio, acá hay que mover el tema, o el Answer queda etiquetado
# con un ítem que no es el que la persona respondió.
#
# Los tres caen en el cinturón BLANCO de su curso, y eso es lo que importa: no
# hace falta que sea el primer tema de la unidad, porque sembrar un tema blanco
# no adelanta contenido y la primera sesión desbloquea el resto igual
# (_ensure_active_units).
#
# Probabilidad supo ser la excepción: su ejercicio era Laplace, cinturón AZUL, y
# sembrarlo habría metido un ítem del azul en la primera sesión, eso sí fuera de
# orden — se registraba el Answer sin tocar UnitState. Ahora es blanco/conteo y
# la excepción no hace falta.
_INTRO_ITEM_BY_COURSE: dict[str, tuple[TopicKey, str]] = {
    "analisis": (TopicKey(belt=Belt.WHITE, topic="definition"), "LEXI"),
    "algebra": (TopicKey(belt=Belt.WHITE, topic="absolute_value"), "RESL"),
    "probabilidad": (TopicKey(belt=Belt.WHITE, topic="reglas"), "FORM"),
}
# Vacío desde que probabilidad dejó de apuntar al azul. Se conserva el mecanismo
# porque el día que un ejercicio del wizard vuelva a caer fuera del blanco, esta
# es la salida.
_INTRO_SEEDLESS_COURSES: set[str] = set()
# Fallback para cursos no mapeados (o datos viejos sin curso).
_INTRO_ITEM_DEFAULT = _INTRO_ITEM_BY_COURSE["analisis"]


def seed_intro_item(
    user_id: int,
    course_id: int,
    correct: bool,
    db: DBSession,
    *,
    attempts: int | None = None,
    response_time_ms: int | None = None,
) -> None:
    """Persiste el resultado del ejercicio de prueba del onboarding sobre el primer
    ítem del curso, aplicándole el mismo update SM-2 que una respuesta real.

    Acierto al primer intento (calidad 5) lo agenda para mañana, así queda fuera de
    la primera sesión. Fallo (calidad 0) lo deja pendiente para hoy, así aparece en
    la primera sesión. Crea las units del tema si todavía no existen; el resto de los
    temas los desbloquea la primera sesión (_ensure_active_units).

    Los cursos en _INTRO_SEEDLESS_COURSES saltean todo el seed de UnitState (su
    ejercicio de prueba no es el primer ítem del curso) y solo registran la
    respuesta.

    Además deja una fila en Answer (con una Session sintética mode="onboarding")
    para poder auditar después intentos y tiempo de respuesta junto con el resto
    de las respuestas. No otorga XP ni cuenta para el progreso real del usuario
    más allá del seed de UnitState de arriba."""
    course = db.query(Course).filter(Course.id == course_id).first()
    course_slug = course.slug if course else ""
    intro_item, intro_type = _INTRO_ITEM_BY_COURSE.get(
        course_slug, _INTRO_ITEM_DEFAULT
    )

    if course_slug not in _INTRO_SEEDLESS_COURSES:
        if not _topic_has_any_units(user_id, course_id, intro_item, db):
            # El alta termina justo cuando el dashboard ya está pidiendo
            # /user/progress de los tres cursos, así que estas mismas units se
            # pueden estar creando en paralelo. Si nos ganaron de mano, la
            # búsqueda de abajo encuentra las de ellos igual.
            with _tolerating_duplicates(db):
                _create_topic_units(user_id, course_id, intro_item, db)

        row = db.query(UnitState).filter(
            UnitState.user_id == user_id,
            UnitState.course_id == course_id,
            UnitState.belt == intro_item.belt.value,
            UnitState.topic == intro_item.topic,
            UnitState.exercise_type == intro_type,
        ).first()
        if row is None:
            return

        new_state = update_unit_state(
            SM2UnitState(), 5 if correct else 0, today=user_today(db, user_id)
        )
        row.phase = new_state.phase
        row.step_index = new_state.step_index
        row.ease_factor = new_state.ease_factor
        row.interval_days = new_state.interval
        row.repetitions = new_state.repetitions
        row.next_due = new_state.next_review
        row.attempted = True
        row.last_reviewed_at = datetime.utcnow()

    onboarding_session = SessionModel(
        user_id=user_id,
        course_id=course_id,
        mode="onboarding",
        exercises_total=1,
        exercises_correct=1 if correct else 0,
        started_at=datetime.utcnow(),
        finished_at=datetime.utcnow(),
        iteration=_get_course_progress(user_id, course_id, db).iteration,
    )
    db.add(onboarding_session)
    db.flush()

    db.add(Answer(
        session_id=onboarding_session.id,
        user_id=user_id,
        course_id=course_id,
        exercise_id=None,
        exercise_external_id=None,
        belt=intro_item.belt.value,
        topic=intro_item.topic,
        exercise_type=intro_type,
        is_correct=correct,
        response_time_ms=response_time_ms,
        quality_score=quality_from_attempts(attempts) if attempts is not None else None,
        xp_earned=0,
        xp_base=0,
        answered_at=datetime.utcnow(),
        iteration=onboarding_session.iteration,
    ))
    db.commit()


def _aggregate_topic_progress(
    rows: list[UnitState],
    expected_types: list[str],
    today: date,
) -> dict:
    """Roll per-unit state up into the topic-shaped progress dict the UI expects."""
    # Maestría/status/progress se calculan SOLO sobre units no-catchup: un ítem
    # agregado después (catch-up) se aprende como repaso extra y no debe bajar de
    # "dominado" un tema ya dominado ni inflar 'mastered' al graduarse.
    mastery_rows = [r for r in rows if not r.is_catchup]
    catchup_types = {r.exercise_type for r in rows if r.is_catchup}
    mastery_types = [et for et in expected_types if et not in catchup_types]
    total_types = len(mastery_types)
    mastered = sum(1 for r in mastery_rows if r.phase == "review")
    attempted = any(r.attempted for r in mastery_rows)
    # 'pending' incluye TODAS las filas (la catch-up vencida hoy es accionable).
    pending = any(r.next_due and r.next_due <= today for r in rows)

    if total_types == 0:
        status = "nuevo"
    elif mastered == total_types and total_types > 0:
        status = "dominado"
    elif not attempted:
        status = "nuevo"
    else:
        status = "aprendiendo"

    next_dues = [r.next_due for r in rows if r.next_due is not None]
    next_review = min(next_dues).isoformat() if next_dues else None
    aggregate_phase = "review" if (
        total_types > 0 and mastered == total_types
    ) else "learning"

    rows_by_type = {r.exercise_type: r for r in rows}
    skills = []
    for et in expected_types:
        row = rows_by_type.get(et)
        skills.append(
            {
                "exercise_type": et,
                "state": _unit_state(row),
                "next_review": (
                    row.next_due.isoformat() if row and row.next_due else None
                ),
            }
        )

    suspended = bool(rows) and all(r.suspended for r in rows)

    return {
        "phase": aggregate_phase,
        "step_index": mastered,
        "status": status,
        "progress": f"{mastered}/{total_types}",
        "is_pending": pending,
        "attempted": attempted,
        "next_review": next_review,
        "failed": False,
        "suspended": suspended,
        "skills": skills,
    }


def _unit_state(row: UnitState | None) -> str:
    """Per-exercise-type learning state for the UI pills."""
    if row is None or not row.attempted:
        return "sin_empezar"
    if row.phase == "review":
        return "dominado"
    return "aprendiendo"


def _exercise_to_dict(ex: ExerciseInSession) -> dict:
    return {
        "id": ex.exercise_id,
        "external_id": ex.external_id,
        "exercise_type": ex.exercise_type,
        "question": ex.question,
        "options": ex.options,
        "correct_index": ex.correct_index,
        "has_math": ex.has_math,
        "topic": ex.topic_key.topic,
        "belt": ex.topic_key.belt.value,
        "graph_fn": ex.graph_fn,
        "graph_view": ex.graph_view,
        "graph_shade": ex.graph_shade,
        "graph_free_aspect": ex.graph_free_aspect,
        "table": ex.table,
        "feedback_correct": ex.feedback_correct,
        "feedback_incorrect": ex.feedback_incorrect,
        "explanation": ex.explanation,
    }


def _shuffle_options(ex: dict) -> tuple[list, int, list | None, dict | None]:
    """Shuffle options preserving every per-option array aligned with them.

    Hay dos estructuras paralelas a `options` que se tienen que permutar con el
    MISMO orden, o quedan pegadas a la opción equivocada:

    - `feedback_incorrect`: array por opción (null en el índice correcto).
    - `table.reveal.by_option`: la columna (o la celda) que pinta cada opción.

    Las dos fallan en silencio si no se permutan: no hay error, solo una pista o
    una tabla que corresponde a otra opción.
    """
    order = list(range(len(ex["options"])))
    random.shuffle(order)
    shuffled = [ex["options"][i] for i in order]
    new_correct_index = order.index(ex["correct_index"])
    feedback = ex.get("feedback_incorrect")
    shuffled_feedback = (
        [feedback[i] for i in order] if isinstance(feedback, list) else feedback
    )
    return shuffled, new_correct_index, shuffled_feedback, _permute_table(ex.get("table"), order)


def _permute_table(table: dict | None, order: list[int]) -> dict | None:
    """Reorder `table.reveal.by_option` to match a shuffled `options` array."""
    if not isinstance(table, dict):
        return None
    reveal = table.get("reveal")
    if not isinstance(reveal, dict):
        return table
    by_option = reveal.get("by_option")
    if not isinstance(by_option, list) or len(by_option) != len(order):
        return table
    return {
        **table,
        "reveal": {**reveal, "by_option": [by_option[i] for i in order]},
    }


def _build_exercise(
    idx: int,
    unit_key: UnitKey,
    course_id: int,
    db: DBSession,
    user_id: int,
    exclude_by_unit: dict[UnitKey, set[str]] | None = None,
    table_boost: float = 1.0,
    require_table: bool = False,
) -> ExerciseInSession:
    # Se pasa el set REAL (no una copia): get_exercise_db lo vacía si esta
    # sesión ya agotó el pool de la unidad, para arrancar otra pasada completa.
    extra_exclude = (
        exclude_by_unit.setdefault(unit_key, set())
        if exclude_by_unit is not None
        else set()
    )
    ex = get_exercise_db(
        course_id,
        unit_key.belt.value,
        unit_key.topic,
        unit_key.exercise_type,
        db,
        user_id,
        extra_exclude=extra_exclude,
        table_boost=table_boost,
        require_table=require_table,
    )
    if ex.get("external_id"):
        extra_exclude.add(ex["external_id"])
    shuffled, new_correct_index, shuffled_feedback, shuffled_table = _shuffle_options(ex)
    return ExerciseInSession(
        exercise_id=f"ex_{idx:03d}",
        unit_key=unit_key,
        question=ex["question"],
        options=shuffled,
        correct_index=new_correct_index,
        feedback_correct=ex["feedback_correct"],
        feedback_incorrect=shuffled_feedback,
        has_math=ex.get("has_math", False),
        graph_fn=ex.get("graph_fn", ""),
        graph_view=ex.get("graph_view"),
        graph_shade=ex.get("graph_shade"),
        graph_free_aspect=bool(ex.get("graph_free_aspect", False)),
        table=shuffled_table,
        explanation=ex.get("explanation"),
        external_id=ex.get("external_id", ""),
    )


def _build_exercise_from_external_id(
    idx: int, external_id: str, course_id: int, db: DBSession,
) -> ExerciseInSession | None:
    """Reconstruye un slot de sesión a partir del external_id ya servido
    (`sessions.served_external_ids`), en vez de volver a sortear con
    `get_exercise_db`. El orden de las opciones no necesita coincidir con el
    original: la identidad autoritativa del ejercicio siempre fue el
    external_id (ver comentario en record_answer_db), nunca el orden mostrado."""
    row = db.query(Exercise).filter(
        Exercise.course_id == course_id, Exercise.external_id == external_id,
    ).first()
    if row is None:
        return None
    ex = _row_to_dict(row)
    unit_key = UnitKey(belt=Belt(row.belt), topic=row.topic, exercise_type=row.exercise_type)
    shuffled, new_correct_index, shuffled_feedback, shuffled_table = _shuffle_options(ex)
    return ExerciseInSession(
        exercise_id=f"ex_{idx:03d}",
        unit_key=unit_key,
        question=ex["question"],
        options=shuffled,
        correct_index=new_correct_index,
        feedback_correct=ex["feedback_correct"],
        feedback_incorrect=shuffled_feedback,
        has_math=ex.get("has_math", False),
        graph_fn=ex.get("graph_fn", ""),
        graph_view=ex.get("graph_view"),
        graph_shade=ex.get("graph_shade"),
        graph_free_aspect=bool(ex.get("graph_free_aspect", False)),
        table=shuffled_table,
        explanation=ex.get("explanation"),
        external_id=ex.get("external_id", ""),
    )


def _rows_to_unit_states(
    rows: list[UnitState],
    today: date,
) -> tuple[dict[UnitKey, SM2UnitState], dict[UnitKey, bool]]:
    states: dict[UnitKey, SM2UnitState] = {}
    attempted: dict[UnitKey, bool] = {}
    for row in rows:
        uk = UnitKey(
            belt=Belt(row.belt),
            topic=row.topic,
            exercise_type=row.exercise_type,
        )
        states[uk] = SM2UnitState(
            phase=row.phase,
            step_index=row.step_index,
            ease_factor=row.ease_factor,
            interval=row.interval_days,
            repetitions=row.repetitions,
            next_review=row.next_due or today,
            recent_results=row.recent_results or "",
        )
        attempted[uk] = row.attempted
    return states, attempted


# ── Empuje inicial de los ejercicios con tabla ────────────────────────────────
# El formato tabla es el más nuevo y el que mejor engancha, pero en el banco es
# minoría (en probabilidad, 9 de 206 del blanco), así que con sorteo uniforme el
# 64% de los usuarios nuevos no veía ninguno en su primera sesión — justo la
# sesión donde se juega la retención.
#
# La corrección es un peso que decae linealmente: x6 en la primera sesión del
# curso, x1 (sin empuje) de la sesión 10 en adelante. Es deliberadamente un
# sesgo de SORTEO y no una cuota: el ciclo por ítem (ver
# exercise_bank.get_exercise_db) ya garantiza que cada ejercicio del pool se
# sirve una sola vez por ciclo, así que esto cambia el ORDEN en que aparecen,
# nunca cuáles. No distorsiona las proporciones de largo plazo ni "gasta" el
# banco: adelanta las tablas y se autocorrige solo.
#
# Agnóstico del curso: donde no hay tablas todos los pesos valen 1 y no cambia
# nada, así que álgebra y análisis lo heredan gratis cuando tengan las suyas.
TABLE_BOOST_MAX = 6.0
TABLE_BOOST_SESSIONS = 10


def _table_boost(user_id: int, course_id: int, db: DBSession) -> tuple[float, bool]:
    """Peso de los ejercicios con tabla y si hay que garantizar uno.

    Devuelve `(peso, garantizar)`. `garantizar` es True solo en la primera
    sesión del curso: el peso por sí solo deja ~13% de usuarios sin ver ninguna
    tabla, y la primera sesión es demasiado cara como para dejarla al azar.

    La cuenta es de sesiones `main` TERMINADAS: las de onboarding son sintéticas
    (mode="onboarding") y una sesión abandonada no debería gastar el empuje."""
    hechas = (
        db.query(func.count(SessionModel.id))
        .filter(
            SessionModel.user_id == user_id,
            SessionModel.course_id == course_id,
            SessionModel.mode == "main",
            SessionModel.finished_at.isnot(None),
        )
        .scalar()
    ) or 0
    n = hechas + 1  # la sesión que se está armando
    if n >= TABLE_BOOST_SESSIONS:
        return 1.0, False
    tramo = (TABLE_BOOST_SESSIONS - n) / (TABLE_BOOST_SESSIONS - 1)
    return 1.0 + (TABLE_BOOST_MAX - 1.0) * tramo, n == 1


ACTIVE_CAP_DEFAULTS = {
    "analisis": 11,
    "probabilidad": 11,
    "algebra": 12,
}
ACTIVE_CAP_DEFAULT_FALLBACK = 18

# Límites del "máximo de ejercicios por sesión" configurable.
SESSION_SIZE_MIN = 1
SESSION_SIZE_MAX = 30
SESSION_SIZE_DEFAULT = 3
SESSION_SIZE_RAMP_CEILING = 8


def _adaptive_session_size(user_id: int, course_id: int, db: DBSession) -> int:
    """Rampa de tamaño de sesión: arranca chico (donde la finalización real es
    más alta) y sube con la racha de sesiones terminadas. Ver
    2026-08-26-motor-de-sesiones.md §4 (AUC 0,694 solo con tamaño) y §8."""
    previas = (
        db.query(SessionModel.finished_at, SessionModel.abandoned)
        .filter(
            SessionModel.user_id == user_id,
            SessionModel.course_id == course_id,
            SessionModel.mode == "main",
        )
        .order_by(SessionModel.started_at.asc())
        .all()
    )
    n = len(previas)
    if n == 0:
        return 3
    if n <= 2:
        return 4

    ultima = previas[-1]
    if ultima.abandoned or ultima.finished_at is None:
        return 3  # tras una sesión abandonada, volver a arrancar de a poco

    racha = 0
    for s in reversed(previas):
        if s.finished_at is not None and not s.abandoned:
            racha += 1
        else:
            break
    # Las primeras 3 terminadas seguidas son las que habilitan la base (5);
    # de ahí en más, +1 cada 3 adicionales.
    return min(5 + max(0, racha - 3) // 3, SESSION_SIZE_RAMP_CEILING)


def _lookup_course_progress(
    user_id: int, course_id: int, db: DBSession
) -> CourseProgress | None:
    return db.query(CourseProgress).filter(
        CourseProgress.user_id == user_id,
        CourseProgress.course_id == course_id,
    ).first()


def _get_course_progress(user_id: int, course_id: int, db: DBSession) -> CourseProgress:
    """Fila de CourseProgress del usuario para el curso, creada lazy con defaults.

    Tolera la creación concurrente: ver _tolerating_duplicates."""
    cp = _lookup_course_progress(user_id, course_id, db)
    if cp is not None:
        return cp

    slug = _get_course_slug(course_id, db)
    with _tolerating_duplicates(db) as conflicto:
        db.add(CourseProgress(
            user_id=user_id,
            course_id=course_id,
            iteration=1,
            active_cap=ACTIVE_CAP_DEFAULTS.get(slug, ACTIVE_CAP_DEFAULT_FALLBACK),
            session_size=SESSION_SIZE_DEFAULT,
        ))
    # La fila tiene que quedar guardada sí o sí: /user/progress es de solo
    # lectura y su sesión se cierra sin commitear, así que sin esto la creación
    # se perdería en cada request.
    db.commit()

    cp = _lookup_course_progress(user_id, course_id, db)
    if cp is None:
        # El choque no fue por la fila que queríamos crear: es otro bug y
        # taparlo lo dejaría invisible, como pasó con este.
        raise conflicto.error or RuntimeError(
            f"no se pudo crear course_progress (user={user_id}, course={course_id})"
        )
    return cp


def _active_cap(user_id: int, course_id: int, db: DBSession) -> int:
    return _get_course_progress(user_id, course_id, db).active_cap


def _active_unit_count(
    user_id: int,
    course_id: int,
    db: DBSession,
    *,
    current: set[tuple[str, str, str]] | None = None,
) -> int:
    """Units activas = en fase de aprendizaje (nuevo + aprendiendo), no graduadas
    ni suspendidas. Las suspendidas liberan cupo (se ceden a temas siguientes).

    Excluye units cuyo exercise_type ya no tiene contenido en `exercises`
    (topic/skill podado del catálogo): sin esto, esas filas huérfanas seguían
    ocupando cupo del active_cap para siempre, aunque la grilla ya no las
    mostrara."""
    current = current if current is not None else _current_exercise_types(course_id, db)
    rows = db.query(UnitState).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
        UnitState.phase != "review",
        UnitState.suspended.is_(False),
    ).all()
    return sum(1 for r in rows if (r.belt, r.topic, r.exercise_type) in current)


def _fill_catchup_units(
    user_id: int,
    course_id: int,
    db: DBSession,
    *,
    types: dict[tuple[str, str], list[str]] | None = None,
) -> None:
    """Rellena exercise_types faltantes en temas que el usuario YA tocó
    (tiene ≥1 fila). Cubre el caso de agregar un skill nuevo a un tema activo
    (p.ej. GRAF agregado retro a funciones ya en uso). Exento del tope
    ACTIVE_CAP: se desbloquean de inmediato y vencen hoy para que sean
    repasables ya.

    NO crea filas para temas con 0 filas del usuario: los temas nuevos o
    renombrados los desbloquea _ensure_active_units cuando la progresión
    normal alcanza esa posición del catálogo, respetando el tope."""
    topic_keys = _all_topic_keys(course_id, db)
    types = types if types is not None else course_exercise_types(course_id, db)

    rows = db.query(UnitState).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
    ).all()
    existing = {(r.belt, r.topic, r.exercise_type) for r in rows}
    topics_with_units = {(r.belt, r.topic) for r in rows}

    today = user_today(db, user_id)
    faltantes = [
        (tk, et)
        for tk in topic_keys
        # Solo rellenar temas que el usuario ya tocó: evita auto-desbloquear
        # temas nuevos (o keys renombradas) que quedan "atrás" en el catálogo.
        if (tk.belt.value, tk.topic) in topics_with_units
        for et in sorted(types.get((tk.belt.value, tk.topic), []))
        if (tk.belt.value, tk.topic, et) not in existing
    ]
    if not faltantes:
        return

    # Si otra request del mismo usuario ya las creó, sus filas valen igual que
    # las nuestras (ver _tolerating_duplicates).
    with _tolerating_duplicates(db):
        for tk, et in faltantes:
            db.add(UnitState(
                user_id=user_id,
                course_id=course_id,
                belt=tk.belt.value,
                topic=tk.topic,
                exercise_type=et,
                phase="learning",
                step_index=0,
                ease_factor=2.5,
                interval_days=1,
                repetitions=0,
                next_due=today,
                attempted=False,
                is_catchup=True,
            ))
    db.commit()


def _ensure_active_units(
    user_id: int,
    course_id: int,
    db: DBSession,
    *,
    types: dict[tuple[str, str], list[str]] | None = None,
    cap: int | None = None,
) -> None:
    """Desbloquea temas en orden de catálogo respetando un máximo ESTRICTO de
    `active_cap` (configurable por usuario+curso; el default sale de
    ACTIVE_CAP_DEFAULTS según el curso) units en fase de
    aprendizaje. Como un tema se desbloquea entero (todos sus exercise_types de
    golpe), solo se introduce el siguiente tema si entra completo sin pasarse del
    cap; si no entra, se espera a que gradúen units y se liberen cupos. A medida
    que las units graduan (pasan a 'review') se vuelven a desbloquear temas hasta
    volver a llenar el cap."""
    types = types if types is not None else course_exercise_types(course_id, db)
    current_types = _current_exercise_types(course_id, db, types=types)

    # Catch-up primero (exento del tope): ítems que quedaron detrás del frontier
    # ya desbloqueado deben aparecer aunque haya >=cap units activas.
    _fill_catchup_units(user_id, course_id, db, types=types)

    cap = cap if cap is not None else _active_cap(user_id, course_id, db)

    # Units del usuario de una sola vez: de acá salen tanto cuántas están activas
    # (ocupando cupo) como qué temas ya tiene desbloqueados.
    rows = db.query(
        UnitState.belt, UnitState.topic, UnitState.exercise_type,
        UnitState.phase, UnitState.suspended,
    ).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
    ).all()
    live = [r for r in rows if (r.belt, r.topic, r.exercise_type) in current_types]
    active = sum(1 for r in live if r.phase != "review" and not r.suspended)
    if active >= cap:
        return
    seen_topics = {(r.belt, r.topic) for r in live}
    a_desbloquear = []
    for tk in _all_topic_keys(course_id, db):
        if active >= cap:
            break
        if (tk.belt.value, tk.topic) in seen_topics:
            continue
        topic_types = types.get((tk.belt.value, tk.topic), [])
        # Mantener el orden del catálogo: si el próximo tema no entra completo,
        # frenar (no saltearlo) para no desbloquear temas fuera de orden.
        if active + len(topic_types) > cap:
            break
        a_desbloquear.append(tk)
        active += len(topic_types)
    if not a_desbloquear:
        return

    # Desbloquear es idempotente: si otra request desbloqueó los mismos temas
    # primero, la BD queda igual que si lo hubiéramos hecho nosotros.
    with _tolerating_duplicates(db):
        for tk in a_desbloquear:
            _create_topic_units(user_id, course_id, tk, db, types=types)
    db.commit()


def _load_unit_states(
    user_id: int,
    course_id: int,
    db: DBSession,
) -> tuple[dict[UnitKey, SM2UnitState], dict[UnitKey, bool]]:
    # Las suspendidas se excluyen de sesiones, resumen y cálculo de maestría.
    # Las huérfanas (exercise_type podado del catálogo) también: build_session
    # las podía elegir como candidatas y `get_exercise_db` no encontraba nada
    # que servir.
    current = _current_exercise_types(course_id, db)
    rows = db.query(UnitState).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
        UnitState.suspended.is_(False),
    ).all()
    rows = [r for r in rows if (r.belt, r.topic, r.exercise_type) in current]
    return _rows_to_unit_states(rows, user_today(db, user_id))


# ── Public API ────────────────────────────────────────────────────────────────

def create_session_db(user_id: int, course_id: int, db: DBSession) -> dict:
    """Create a new session, picking the next batch of exercises with the SR algorithm."""
    # One main session per day, EXCEPT the user can keep reviewing while there
    # are still pending (due) items. Once nothing's due, the daily gate applies.
    if _has_main_session_today(user_id, course_id, db) and not _has_pending_items(
        user_id, course_id, db
    ):
        raise DailySessionLimitError(
            "Ya completaste tus repasos de hoy. Volvé mañana."
        )

    _ensure_active_units(user_id, course_id, db)
    unit_states, unit_attempted = _load_unit_states(user_id, course_id, db)

    # El desbloqueo lo maneja _ensure_active_units (tope `active_cap` por
    # usuario+curso, ver ACTIVE_CAP_DEFAULTS); la sesión solo arma con lo que
    # está activo/vencido, sin introducir temas extra. El tope de ejercicios por
    # sesión es configurable por usuario+curso (session_size).
    course_progress = _get_course_progress(user_id, course_id, db)
    if course_progress.session_size_auto:
        course_progress.session_size = _adaptive_session_size(user_id, course_id, db)
    session_units = build_session(
        unit_states,
        unit_attempted=unit_attempted,
        config=SM2Config(max_session_exercises=course_progress.session_size),
    )

    boost, garantizar = _table_boost(user_id, course_id, db)
    exclude_by_unit: dict[UnitKey, set[str]] = {}
    exercises = []
    for idx, su in enumerate(session_units):
        exercises.append(_build_exercise(
            idx, su.key, course_id, db, user_id, exclude_by_unit,
            table_boost=boost,
            # La garantía es "el primer slot que pueda dar tabla la da": se
            # apaga apenas se usa, así que un segundo ítem con tabla en el
            # pool queda solo con el empuje de `boost`, no forzado también.
            require_table=garantizar,
        ))
        if exercises[-1].table is not None:
            garantizar = False

    db_session = SessionModel(
        user_id=user_id,
        course_id=course_id,
        started_at=datetime.utcnow(),
        exercises_total=len(exercises),
        mode="main",
        iteration=course_progress.iteration,
        # Identidad de lo servido, en orden, desde el arranque: sin esto una
        # sesión abandonada sin ninguna respuesta no deja rastro de qué vio el
        # usuario, y una caché fría re-sortea en vez de reconstruir (ver
        # _reconstruct_session_state). 2026-08-26-motor-de-sesiones.md §4-bis.
        served_external_ids=json.dumps([ex.external_id for ex in exercises]),
    )
    db.add(db_session)
    db.flush()
    session_id_db = db_session.id
    db.commit()

    session_id_str = str(session_id_db)
    _sweep_sessions()
    _sessions[session_id_str] = SessionState(
        session_id=session_id_str,
        user_name="",
        unit_states=unit_states,
        exercises=exercises,
    )

    from feedback_survey import assign_survey
    survey = assign_survey(user_id, course_id, exercises, db)

    return {
        "session_id": session_id_str,
        "user_name": "",
        "total": len(exercises),
        "mode": "main",
        "exercises": [_exercise_to_dict(ex) for ex in exercises],
        "survey": survey,
    }


def create_practice_session_db(
    user_id: int,
    course_id: int,
    items: list[dict],
    count: int,
    db: DBSession,
) -> dict:
    """Practice mode: random exercises from selected (belt, topic) items, no SR tracking."""
    slug = _get_course_slug(course_id, db)
    all_catalogs = load_belt_catalogs(slug)
    types = course_exercise_types(course_id, db)

    candidate_units: list[UnitKey] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        belt = item.get("belt")
        topic = item.get("topic")
        try:
            belt_enum = Belt(belt)
        except ValueError:
            raise ValueError(f"Unidad desconocida: {belt}")
        if belt_enum not in all_catalogs:
            raise ValueError(f"Unidad desconocida: {belt}")
        key = (belt_enum.value, topic)
        if key in seen:
            continue
        seen.add(key)
        for et in types.get((belt_enum.value, topic), []):
            candidate_units.append(
                UnitKey(belt=belt_enum, topic=topic, exercise_type=et)
            )

    if not candidate_units:
        raise ValueError("No hay ejercicios disponibles para los temas seleccionados.")

    sampled = random.choices(candidate_units, k=count)
    exclude_by_unit: dict[UnitKey, set[str]] = {}
    exercises = [
        _build_exercise(idx, uk, course_id, db, user_id, exclude_by_unit)
        for idx, uk in enumerate(sampled)
    ]

    db_session = SessionModel(
        user_id=user_id, course_id=course_id,
        started_at=datetime.utcnow(), exercises_total=len(exercises),
        mode="practice",
        iteration=_get_course_progress(user_id, course_id, db).iteration,
    )
    db.add(db_session)
    db.flush()
    db.commit()

    session_id_str = str(db_session.id)
    _sweep_sessions()
    _sessions[session_id_str] = SessionState(
        session_id=session_id_str,
        user_name="",
        unit_states={uk: SM2UnitState() for uk in set(sampled)},
        exercises=exercises,
    )

    return {
        "session_id": session_id_str,
        "user_name": "",
        "total": len(exercises),
        "mode": "practice",
        "exercises": [_exercise_to_dict(ex) for ex in exercises],
    }


def create_test_session_db(
    user_id: int,
    course_id: int,
    items: list[dict],
    db: DBSession,
    shuffle: bool = True,
    filters: dict | None = None,
) -> dict:
    """QA/test mode: play through EVERY exercise in each selected item
    (belt, topic, exercise_type). No SR tracking.

    `items` is a list of {belt, topic, exercise_type} dicts.
    `filters` may contain `has_math`, `has_graph` and `has_table` (bool) to
    narrow the exercise set (all default to no-op).
    """
    only_math = bool(filters and filters.get("has_math"))
    only_graph = bool(filters and filters.get("has_graph"))
    only_table = bool(filters and filters.get("has_table"))

    exercises: list[ExerciseInSession] = []
    idx = 0
    for item in items:
        belt = item.get("belt")
        topic = item.get("topic")
        et = item.get("exercise_type")
        try:
            belt_enum = Belt(belt)
        except ValueError:
            raise ValueError(f"Unidad desconocida: {belt}")
        unit_key = UnitKey(belt=belt_enum, topic=topic, exercise_type=et)
        rows = list_exercises_db(course_id, belt, topic, et, db)
        for ex in rows:
            if only_math and not ex.get("has_math"):
                continue
            if only_graph and not ex.get("graph_fn"):
                continue
            if only_table and not ex.get("table"):
                continue
            shuffled, new_correct_index, shuffled_feedback, shuffled_table = _shuffle_options(ex)
            exercises.append(
                ExerciseInSession(
                    exercise_id=f"ex_{idx:03d}",
                    unit_key=unit_key,
                    question=ex["question"],
                    options=shuffled,
                    correct_index=new_correct_index,
                    feedback_correct=ex["feedback_correct"],
                    feedback_incorrect=shuffled_feedback,
                    has_math=ex.get("has_math", False),
                    graph_fn=ex.get("graph_fn", ""),
                    graph_view=ex.get("graph_view"),
                    graph_shade=ex.get("graph_shade"),
                    graph_free_aspect=bool(ex.get("graph_free_aspect", False)),
                    table=shuffled_table,
                    explanation=ex.get("explanation"),
                    external_id=ex.get("external_id", ""),
                )
            )
            idx += 1

    if not exercises:
        raise ValueError("No hay ejercicios para los items seleccionados.")

    if shuffle:
        random.shuffle(exercises)

    db_session = SessionModel(
        user_id=user_id, course_id=course_id,
        started_at=datetime.utcnow(), exercises_total=len(exercises),
        mode="test",
        iteration=_get_course_progress(user_id, course_id, db).iteration,
    )
    db.add(db_session)
    db.flush()
    db.commit()

    session_id_str = str(db_session.id)
    _sweep_sessions()
    _sessions[session_id_str] = SessionState(
        session_id=session_id_str,
        user_name="",
        unit_states={ex.unit_key: SM2UnitState() for ex in exercises},
        exercises=exercises,
    )

    return {
        "session_id": session_id_str,
        "user_name": "",
        "total": len(exercises),
        "mode": "test",
        "exercises": [_exercise_to_dict(ex) for ex in exercises],
    }


def _reconstruct_session_state(
    session_id_db: int,
    user_id: int,
    course_id: int,
    db: DBSession,
) -> SessionState:
    """Rebuild SessionState from DB when the in-memory cache is cold."""
    rows = db.query(UnitState).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
    ).all()
    unit_states, unit_attempted = _rows_to_unit_states(rows, user_today(db, user_id))

    # Si la sesión guardó qué se sirvió (served_external_ids, desde este
    # cambio), reconstruir leyendo eso en vez de volver a sortear con
    # build_session — que podía darle a la persona OTRA sesión, con otros
    # ítems y otros ejercicios, tras un reinicio del proceso. Sesiones viejas
    # (sin la columna poblada) caen al comportamiento anterior.
    db_session = db.query(SessionModel).filter(SessionModel.id == session_id_db).first()
    served_ids = []
    if db_session and db_session.served_external_ids:
        try:
            served_ids = json.loads(db_session.served_external_ids)
        except (TypeError, ValueError):
            served_ids = []

    if served_ids:
        exercises = []
        for idx, external_id in enumerate(served_ids):
            rebuilt = _build_exercise_from_external_id(idx, external_id, course_id, db)
            if rebuilt is not None:
                exercises.append(rebuilt)
    else:
        session_units = build_session(unit_states, unit_attempted=unit_attempted)
        exclude_by_unit: dict[UnitKey, set[str]] = {}
        exercises = [
            _build_exercise(idx, su.key, course_id, db, user_id, exclude_by_unit)
            for idx, su in enumerate(session_units)
        ]

    streak = 0
    recent = (
        db.query(Answer.is_correct)
        .filter(Answer.session_id == session_id_db)
        .order_by(Answer.answered_at.desc())
        .all()
    )
    for row in recent:
        if row.is_correct:
            streak += 1
        else:
            break

    return SessionState(
        session_id=str(session_id_db),
        user_name="",
        unit_states=unit_states,
        exercises=exercises,
        streak=streak,
    )


def _unit_difficulty(
    user_id: int,
    course_id: int,
    unit_key: UnitKey,
    db: DBSession,
) -> float:
    """Multiplicador de dificultad personal del ítem (×0.5 dominado → ×1.25 le
    cuesta), según la precisión al primer intento (quality_score == 5) en las
    últimas DIFFICULTY_WINDOW respuestas del usuario en ese ítem."""
    rows = (
        db.query(Answer.quality_score)
        .filter(
            Answer.user_id == user_id,
            Answer.course_id == course_id,
            Answer.belt == unit_key.belt.value,
            Answer.topic == unit_key.topic,
            Answer.exercise_type == unit_key.exercise_type,
        )
        .order_by(Answer.id.desc())
        .limit(DIFFICULTY_WINDOW)
        .all()
    )
    samples = len(rows)
    if samples < DIFFICULTY_MIN_SAMPLES:
        return 1.0
    first_try_rate = sum(1 for (q,) in rows if q == 5) / samples
    return difficulty_multiplier(first_try_rate, samples)


def _actualizar_elo(
    db: DBSession,
    *,
    user: User | None,
    course_id: int,
    external_id: str | None,
    unit_key: UnitKey,
    mode: str,
    first_try: bool,
) -> None:
    """Mueve el Elo jerárquico: la habilidad de la persona y la dificultad del
    ejercicio y del ítem, las tres en la misma transacción que la respuesta.

    Sale de `record_answer_db` porque es lo único de esas 300 líneas que no
    comparte nada con el resto salvo `first_try`: no lee ni escribe el estado
    SM-2, no toca la XP y no participa del feedback que se devuelve. Adentro era
    un bloque de cuarenta líneas con tres niveles de anidado que había que
    saltear para seguir el hilo de la función.

    Con el mismo criterio P1 de todo el informe (quality_score == 5, no
    is_correct). Solo en repaso real: práctica no tiene target de dificultad
    propio y test es QA — mezclarlos ensucia la señal (ver
    2026-08-26-motor-de-sesiones.md §0/§5/§9). Sin job y sin reentrenamiento.
    """
    if not (user and external_id) or mode in ("practice", "test"):
        return
    ex_row = db.query(Exercise).filter(
        Exercise.course_id == course_id,
        Exercise.external_id == external_id,
    ).first()
    if ex_row is None:
        return
    item_row = db.query(ItemDifficulty).filter(
        ItemDifficulty.course_id == course_id,
        ItemDifficulty.belt == unit_key.belt.value,
        ItemDifficulty.topic == unit_key.topic,
        ItemDifficulty.exercise_type == unit_key.exercise_type,
    ).first()
    if item_row is None:
        item_row = ItemDifficulty(
            course_id=course_id,
            belt=unit_key.belt.value,
            topic=unit_key.topic,
            exercise_type=unit_key.exercise_type,
        )
        db.add(item_row)
        db.flush()
    new_theta, new_beta_x, new_beta_i = elo_update(
        user.ability, ex_row.difficulty, item_row.difficulty,
        user.ability_n, ex_row.difficulty_n, item_row.difficulty_n,
        1 if first_try else 0,
    )
    user.ability = new_theta
    user.ability_n += 1
    ex_row.difficulty = new_beta_x
    ex_row.difficulty_n += 1
    item_row.difficulty = new_beta_i
    item_row.difficulty_n += 1


class PagoDeRespuesta(NamedTuple):
    """Lo que paga una respuesta, repartido en sus partes.

    `base` es antes de los multiplicadores, `earned` es lo que se acredita, y
    `del_empuje` es cuánto de la diferencia la puso el cafecito y no la racha —
    ese último no se puede reconstruir después, y es lo que se guarda en
    `answers.xp_from_boost`.

    `combo` es el contador de aciertos limpios seguidos DENTRO de la sesión, que
    entra y sale porque el bonus depende de él y él de si esta respuesta fue al
    primer intento. Devolverlo en vez de mutar el estado de la sesión adentro
    deja la función entera decidible por sus argumentos.
    """

    base: int
    earned: int
    del_empuje: int
    combo: int


def _repartir_xp(
    db: DBSession,
    *,
    user: User | None,
    user_id: int,
    course_id: int,
    unit_key: UnitKey,
    mode: str,
    en_repaso: bool,
    first_try: bool,
    attempts: int,
    combo: int,
) -> PagoDeRespuesta:
    """Cuánto paga esta respuesta, y de dónde sale cada parte.

    Práctica paga plano y sin ajuste de dificultad (volumen ilimitado a elección
    del usuario), pero sí escala con el multiplicador efectivo — su base es mucho
    menor que la de Repaso, así que no se vuelve farmeable. Repaso paga por
    intento, ponderado por la dificultad personal del ítem (solo 1er intento y
    solo en fase de retención: en aprendizaje la base es menor y plana, ver
    review_xp_base) y el mismo multiplicador.
    """
    streak_mult = streak_multiplier(user.streak_days if user else 0)
    # El empuje de cafecito de su universidad, si hay alguno corriendo. Casi
    # siempre no hay, y averiguarlo es gratis: `hay_empujes` memoriza el "no"
    # unos segundos por proceso (ver xp_boost.multiplier_for_user).
    boost_mult = xp_boost.multiplier_for_user(db, user_id)
    # Los dos multiplicadores se aplican JUNTOS y redondeando una sola vez, y con
    # un tope propio sobre el producto. Ver algorithm/xp.py :: MAX_TOTAL_MULTIPLIER
    # para por qué el tope es 4,0 y no el ×3 del juego.
    mult = effective_multiplier(streak_mult, boost_mult)

    if mode == "practice":
        xp_base, xp_earned = practice_xp_split(first_try, mult)
        # Cuánto de lo cobrado lo puso el empuje y no la racha (ver
        # algorithm/xp.py :: xp_from_boost).
        return PagoDeRespuesta(
            xp_base, xp_earned, xp_from_boost(xp_base, streak_mult, mult), combo
        )

    difficulty = (
        _unit_difficulty(user_id, course_id, unit_key, db)
        if first_try and en_repaso
        else 1.0
    )
    xp_base, xp_earned = review_xp_split(
        attempts, difficulty, mult, learning=not en_repaso
    )
    # Antes del bonus de combo, que es plano: si entrara en la cuenta, el empuje
    # se llevaría el crédito de algo que no multiplicó.
    del_empuje = xp_from_boost(xp_base, streak_mult, mult)

    if not first_try:
        return PagoDeRespuesta(xp_base, xp_earned, del_empuje, 0)

    combo += 1
    if combo % XP_STREAK_INTERVAL == 0:
        # Bonus de combo interno a la sesión: no lo multiplica la racha diaria ni
        # el empuje, así que va a la base (no cuenta como "extra").
        xp_earned += XP_STREAK_BONUS
        xp_base += XP_STREAK_BONUS
    return PagoDeRespuesta(xp_base, xp_earned, del_empuje, combo)


def record_answer_db(
    session_id_db: int,
    user_id: int,
    exercise_id: str,
    answer_index: int,
    attempts: int,
    response_time_s: float,
    db: DBSession,
    exercise_external_id: str | None = None,
) -> dict:
    """Record an answer, update SM-2 state for the unit, return feedback."""
    db_session = db.query(SessionModel).filter(
        SessionModel.id == session_id_db,
        SessionModel.user_id == user_id,
    ).first()
    if not db_session:
        raise KeyError(f"Sesión {session_id_db} no encontrada o no pertenece al usuario.")

    # course_id proviene siempre de la sesión (no del cliente), así respetamos
    # el curso al que la sesión pertenece y evitamos drift entre client/server.
    course_id = db_session.course_id
    session_id_str = str(session_id_db)
    state = _sessions.get(session_id_str)
    if state is None:
        state = _reconstruct_session_state(session_id_db, user_id, course_id, db)
        _sessions[session_id_str] = state

    exercise = next((e for e in state.exercises if e.exercise_id == exercise_id), None)
    if exercise is None:
        raise KeyError(f"Ejercicio '{exercise_id}' no encontrado en la sesión.")

    # El progreso de la sesión no vive en el cliente: el useState del runner se
    # pierde al remontar (back del navegador con el sessionStorage todavía vivo,
    # tab duplicado), y esa segunda pasada re-enviaba los mismos slots — filas
    # duplicadas en answers, exercises_correct > total y XP pagado dos veces
    # (sesiones 995, 1023 y 1114 en producción). Dos guards:
    #
    # 1. Sesión ya cerrada (el summary corrió): nada más que aceptar.
    if db_session.finished_at is not None:
        raise SessionClosedError("La sesión ya terminó.")

    # 2. Slot ya respondido: idempotencia. Se devuelve el resultado original en
    #    vez de error, así un reintento de red del cliente recibe lo mismo que
    #    la primera vez. La clave es el slot y no el external_id porque una
    #    sesión más larga que el pool repite externals legítimamente en slots
    #    distintos. El unique index uq_answers_session_slot es la red de
    #    contención si esto se saltea.
    prior = (
        db.query(Answer)
        .filter(Answer.session_id == session_id_db, Answer.exercise_id == exercise_id)
        .first()
    )
    if prior is not None:
        if prior.is_correct:
            feedback = exercise.feedback_correct
        elif isinstance(exercise.feedback_incorrect, list):
            feedback = exercise.explanation or ""
        else:
            feedback = exercise.feedback_incorrect
        return {
            "correct": prior.is_correct,
            "quality": prior.quality_score,
            "feedback": feedback,
            "xp_earned": prior.xp_earned or 0,
        }

    # Identidad exacta del ejercicio que vio el usuario. La fuente autoritativa es
    # el external_id que reporta el cliente (lo que efectivamente renderizó): la
    # caché en memoria puede enfriarse y _reconstruct_session_state re-sortea los
    # ejercicios al azar, así que el external_id del slot en memoria no es
    # confiable tras un reinicio. Si el cliente no lo manda (compat), caemos al de
    # memoria.
    resolved_external_id = exercise_external_id or exercise.external_id or None

    unit_key = exercise.unit_key
    is_correct = attempts <= 3
    # Solo el acierto limpio (al primer intento) otorga XP de correcta y suma
    # racha. Si hubo al menos un error previo, aunque después se acierte, se
    # otorga el XP de intento (1) y se corta la racha.
    first_try = attempts == 1
    # quality_score guardado (semántica de resumen: 5 ⟺ primer intento).
    quality = quality_from_attempts(attempts)

    current_state = state.unit_states.get(unit_key, SM2UnitState())

    # Calidad que alimenta SM-2 (distinta del quality_score guardado):
    #  - Aprendizaje: solo avanza con acierto al primer intento (5); si no, 0
    #    (reinicia al paso 1 ese mismo día).
    #  - Retención: si acertó al primer intento, la calidad la define el tiempo
    #    (<10s→5, <30s→4, resto→3) y modula el ease factor; si pifió, 0 (repasa
    #    el mismo día sin salir de la fase de retención).
    if current_state.phase == "review":
        sm2_quality = quality_from_time(response_time_s) if first_try else 0
    else:
        sm2_quality = 5 if first_try else 0

    new_state = update_unit_state(
        current_state, sm2_quality, today=user_today(db, user_id)
    )
    state.unit_states[unit_key] = new_state

    user = db.query(User).filter(User.id == user_id).first()

    _actualizar_elo(
        db,
        user=user,
        course_id=course_id,
        external_id=resolved_external_id,
        unit_key=unit_key,
        mode=db_session.mode,
        first_try=first_try,
    )

    pago = _repartir_xp(
        db,
        user=user,
        user_id=user_id,
        course_id=course_id,
        unit_key=unit_key,
        mode=db_session.mode,
        en_repaso=current_state.phase == "review",
        first_try=first_try,
        attempts=attempts,
        combo=state.streak,
    )
    xp_base, xp_earned, xp_del_empuje = pago.base, pago.earned, pago.del_empuje
    state.streak = pago.combo

    db_us = db.query(UnitState).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
        UnitState.belt == unit_key.belt.value,
        UnitState.topic == unit_key.topic,
        UnitState.exercise_type == unit_key.exercise_type,
    ).first()

    # Practice mode is free practice: it only awards XP and must not touch the
    # student's spaced-repetition progress (phase, interval, due date, mastery).
    if db_us and db_session.mode not in ("practice", "test"):
        old_phase = db_us.phase
        db_us.phase = new_state.phase
        db_us.step_index = new_state.step_index
        db_us.ease_factor = new_state.ease_factor
        db_us.interval_days = new_state.interval
        db_us.repetitions = new_state.repetitions
        db_us.next_due = new_state.next_review
        db_us.recent_results = new_state.recent_results
        db_us.attempted = True
        db_us.updated_at = datetime.utcnow()
        db_us.last_reviewed_at = datetime.utcnow()

        if old_phase == "learning" and new_state.phase == "review":
            topic_key = unit_key.topic_key
            # Maestría/promoción ignoran las units catch-up (repaso extra): un ítem
            # agregado después no debe condicionar ni despromocionar lo ya logrado.
            # Ya no otorgan XP propio: solo desbloquean units nuevas.
            expected_types = _mastery_types(user_id, course_id, topic_key, db)
            if is_topic_mastered(state.unit_states, topic_key, expected_types):
                _ensure_active_units(user_id, course_id, db)

    state.xp_session += xp_earned

    db.add(Answer(
        session_id=session_id_db,
        user_id=user_id,
        course_id=course_id,
        exercise_id=exercise_id,
        exercise_external_id=resolved_external_id,
        belt=unit_key.belt.value,
        topic=unit_key.topic,
        exercise_type=unit_key.exercise_type,
        is_correct=is_correct,
        response_time_ms=int(response_time_s * 1000),
        quality_score=quality,
        xp_earned=xp_earned,
        xp_base=xp_base,
        xp_from_boost=xp_del_empuje,
        answered_at=datetime.utcnow(),
        iteration=_get_course_progress(user_id, course_id, db).iteration,
    ))

    # El ejercicio quedó efectivamente completado por el usuario: avanza el
    # ciclo de no-repetición del ítem (ver ItemExerciseCycle).
    mark_exercise_served(
        user_id,
        course_id,
        unit_key.belt.value,
        unit_key.topic,
        unit_key.exercise_type,
        resolved_external_id,
        db,
    )

    if user:
        # Incremento RELATIVO por SQL, no `user.total_xp = leído + n`.
        #
        # La fila se leyó ~130 líneas más arriba y el commit llega recién ahora.
        # Escribir el valor absoluto que se leyó pisa cualquier cosa que haya
        # entrado en el medio, y ya hay dos escritores concurrentes de esta
        # columna: el FEEDBACK_XP de las encuestas (main.py) y —desde que los
        # reclutas cruzan de producto— el 10% que un recluta le acuña a quien lo
        # trajo, que llega como UPDATE crudo desde OTRA transacción.
        #
        # En el minijuego el patrón equivalente es seguro porque el receptor
        # serializa sus propias respuestas con `lock_player` (SELECT … FOR
        # UPDATE); acá no hay nada así, y tomarlo serializaría todo el camino de
        # respuesta por algo que hoy no lo necesita. El incremento relativo
        # resuelve lo mismo sin candado.
        db.query(User).filter(User.id == user_id).update(
            {User.total_xp: User.total_xp + xp_earned}, synchronize_session=False
        )
        # `synchronize_session=False` no toca el identity map, así que el
        # objeto `user` en memoria queda con el valor viejo. Hoy nadie lo lee
        # entre esta línea y el commit —y el commit expira todo, porque
        # `expire_on_commit` está en su default—, así que esto es defensivo: lo
        # que evita es que alguien intercale una lectura acá en el futuro y se
        # lleve el número de antes sin que nada falle.
        db.expire(user, ["total_xp"])
        # Y si alguien trajo a esta persona, cobra su 10%. La XP se ACUÑA: al
        # recluta no se le descuenta nada, cobra exactamente lo mismo que
        # cobraría sin reclutador. Va en la misma transacción que la respuesta,
        # así que o entran las dos cosas o no entra ninguna.
        referrals.acreditar_clasico(db, user, xp_earned)

    if is_correct:
        db_session.exercises_correct = (db_session.exercises_correct or 0) + 1
    db.commit()

    state.results.append({
        "exercise_id": exercise_id,
        "unit_key": unit_key,
        "is_correct": is_correct,
        "quality": quality,
    })

    if is_correct:
        feedback = exercise.feedback_correct
    elif isinstance(exercise.feedback_incorrect, list):
        feedback = exercise.explanation or ""
    else:
        feedback = exercise.feedback_incorrect
    return {
        "correct": is_correct,
        "quality": quality,
        "feedback": feedback,
        "xp_earned": xp_earned,
    }


def _topic_rows_index(
    rows: list[UnitState],
) -> dict[tuple[str, str], list[UnitState]]:
    out: dict[tuple[str, str], list[UnitState]] = {}
    for r in rows:
        out.setdefault((r.belt, r.topic), []).append(r)
    return out


def get_user_progress_db(user_id: int, course_id: int, db: DBSession) -> dict:
    """Return topic-level progress (rolled up from per-unit state) and level info."""
    catalog_keys = _all_topic_keys(course_id, db)

    # El catálogo de ítems del curso, de una sola vez: alimenta el desbloqueo, el
    # `expected` de cada tema y el total de ítems, que antes recorrían la misma
    # lista de temas pegándole a la BD en cada vuelta.
    types = course_exercise_types(course_id, db)
    current_types = _current_exercise_types(course_id, db, types=types)

    cp = _get_course_progress(user_id, course_id, db)
    _ensure_active_units(user_id, course_id, db, types=types, cap=cp.active_cap)

    rows = db.query(UnitState).filter(
        UnitState.user_id == user_id, UnitState.course_id == course_id,
    ).all()
    rows = [r for r in rows if (r.belt, r.topic, r.exercise_type) in current_types]
    rows_by_topic = _topic_rows_index(rows)

    today = user_today(db, user_id)
    topic_states: dict[str, dict] = {}
    total_items = 0
    for key in catalog_keys:
        expected = types.get((key.belt.value, key.topic), [])
        total_items += len(expected)
        topic_rows = rows_by_topic.get((key.belt.value, key.topic))
        if not topic_rows:
            continue
        topic_states[f"{key.belt.value}/{key.topic}"] = _aggregate_topic_progress(topic_rows, expected, today)

    user = db.query(User).filter(User.id == user_id).first()

    # Última sesión del usuario (cualquier curso), para que el dashboard pueda
    # abrir por defecto el curso donde estuvo trabajando.
    last_session = (
        db.query(SessionModel)
        .filter(SessionModel.user_id == user_id)
        .order_by(SessionModel.started_at.desc())
        .first()
    )
    last_course_slug: str | None = None
    if last_session is not None:
        last_course_row = (
            db.query(Course).filter(Course.id == last_session.course_id).first()
        )
        if last_course_row is not None:
            last_course_slug = last_course_row.slug

    # Racha global de días, para mostrar el multiplicador de XP vigente fuera
    # del summary (indicador de Repasar/Practicar). `counted_today` acá refleja
    # si ya se completó alguna sesión hoy (no cuenta un día nuevo como el
    # summary, solo informa).
    si = streak_info(user.streak_days if user else 0)
    streak_counted_today = bool(user and user.streak_last_date == today)

    # El empuje de cafecito de su universidad, si hay alguno corriendo. `None`
    # cuando no hay, que es casi siempre: la tile solo cambia de cara cuando hay
    # algo que contar, y averiguar que no hay nada es gratis (`hay_empujes`
    # memoriza el "no" unos segundos por proceso).
    # Los dos de una sola llamada: preguntarlos separado resolvía dos veces el
    # enrollment y dos veces el candado, y este endpoint lo pide el dashboard
    # para los tres cursos en paralelo.
    empuje = xp_boost.empuje_de_usuario(db, user_id)
    tramos, boost_mult = empuje.tramos, empuje.multiplier
    boost = None
    if tramos and boost_mult > 1.0:
        boost = {
            "multiplier": boost_mult,
            # El que de verdad se cobra, calculado con la MISMA función que usa
            # el que paga (record_answer_db). Si la tile hiciera su propia
            # cuenta, las dos se desincronizan en el primer ajuste y el que
            # pierde la confianza es el número.
            "effective_multiplier": effective_multiplier(si.multiplier, boost_mult),
            # El MÍNIMO de los tramos vigentes, no el máximo: es el instante en
            # que el número que se muestra deja de ser cierto. Con el global y
            # el de su universidad corriendo a la vez, el que vence primero ya
            # baja el multiplicador aunque el otro siga.
            "expires_in_seconds": min(t.expires_in_seconds for t in tramos),
            # La lista entera, porque puede estar cobrando DOS empujes con dos
            # donantes distintos y el número no le pertenece a ninguno.
            "tramos": [
                {
                    "university": t.university,
                    # Lo que este tramo solo multiplica. No suma con el de al
                    # lado: el total ya viene arriba, calculado sobre los
                    # cafecitos sumados (ver BoostTramo).
                    "multiplier": t.multiplier,
                    "cafecitos": t.cafecitos,
                    "donor_name": t.donor_name,
                    "expires_in_seconds": t.expires_in_seconds,
                    "aforo": t.aforo,
                }
                for t in tramos
            ],
        }

    return {
        "topic_states": topic_states,
        "boost": boost,
        "main_session_done_today": _has_main_session_today(user_id, course_id, db),
        "last_course": last_course_slug,
        "active_cap": cp.active_cap,
        "total_items": total_items,
        "iteration": cp.iteration,
        "session_size": cp.session_size,
        "session_size_max": SESSION_SIZE_MAX,
        "streak": {
            "days": si.days,
            "multiplier": si.multiplier,
            "next_threshold": si.next_threshold,
            "next_multiplier": si.next_multiplier,
            "days_to_next": si.days_to_next,
            "is_max": si.is_max,
            "tier_reached": si.tier_reached,
            "prev_multiplier": si.prev_multiplier,
            "counted_today": streak_counted_today,
        },
    }


def get_summary_db(
    session_id_db: int,
    user_id: int,
    db: DBSession,
) -> dict:
    """Finalize the session and return a summary."""
    db_session = db.query(SessionModel).filter(
        SessionModel.id == session_id_db,
        SessionModel.user_id == user_id,
    ).first()
    if not db_session:
        raise KeyError(f"Sesión {session_id_db} no encontrada.")

    answers = db.query(Answer).filter(Answer.session_id == session_id_db).all()
    total = len(answers)
    correct_count = sum(1 for a in answers if a.is_correct)
    # "Correctos" = acertados al primer intento. quality_score == 5 ⟺ attempts == 1
    # (ver quality_from_attempts). El resto, aunque se acierte luego, no cuenta.
    first_try_correct = sum(1 for a in answers if a.quality_score == 5)
    incorrect_count = total - correct_count

    items = [
        {
            "topic": a.topic,
            "belt": a.belt,
            "correct": a.is_correct,
        }
        for a in answers
    ]

    course_id = db_session.course_id
    catalog_keys = _all_topic_keys(course_id, db)

    types = course_exercise_types(course_id, db)
    current_types = _current_exercise_types(course_id, db, types=types)

    rows = db.query(UnitState).filter(
        UnitState.user_id == user_id, UnitState.course_id == course_id,
        UnitState.suspended.is_(False),
    ).all()
    rows = [r for r in rows if (r.belt, r.topic, r.exercise_type) in current_types]
    rows_by_topic = _topic_rows_index(rows)

    failed_in_session: set[tuple[str, str]] = set()
    for a in answers:
        if not a.is_correct:
            failed_in_session.add((a.belt, a.topic))

    today = user_today(db, user_id)
    topic_states: dict[str, dict] = {}

    for key in catalog_keys:
        topic_rows = rows_by_topic.get((key.belt.value, key.topic))
        if not topic_rows:
            continue
        expected = types.get((key.belt.value, key.topic), [])
        ts_dict = _aggregate_topic_progress(topic_rows, expected, today)
        if (key.belt.value, key.topic) in failed_in_session:
            ts_dict["failed"] = True
        topic_states[f"{key.belt.value}/{key.topic}"] = ts_dict

    xp_earned = sum(a.xp_earned or 0 for a in answers)
    xp_base_total = sum(a.xp_base or 0 for a in answers)
    # El extra se reparte en DOS, y no es un detalle de presentación: desde que
    # el cafecito multiplica el XP de clásico, `xp_earned - xp_base` dejó de ser
    # "el bonus por tu racha" y pasó a ser la suma de los dos multiplicadores.
    # El resumen mostraba ese número entero al lado de "×1,2" —el de la racha—,
    # así que con un empuje corriendo la pantalla enseñaba una cuenta que no
    # cerraba con ninguno de los dos.
    #
    # `answers.xp_from_boost` existe justamente porque el aporte del empuje no se
    # puede reconstruir después: acá es de donde sale.
    xp_del_empuje = sum(a.xp_from_boost or 0 for a in answers)
    xp_bonus_total = max(0, xp_earned - xp_base_total)
    xp_bonus_racha = max(0, xp_bonus_total - xp_del_empuje)
    # Solo el primer summary cierra la sesión: cada revisita re-corría esto y
    # pisaba finished_at con la hora actual, destruyendo el timestamp real de
    # finalización (y con él la duración de la sesión). Además, con finished_at
    # puesto record_answer_db rechaza respuestas tardías de un replay.
    if db_session.finished_at is None:
        db_session.finished_at = datetime.utcnow()
    db_session.xp_earned = xp_earned
    # El flush explícito, porque más abajo `session_number` cuenta las sesiones
    # con `finished_at` puesto y tiene que verse a sí misma. Antes lo hacía por
    # accidente el `commit()` que había en el medio; con un solo commit al final
    # no queda nadie que lo garantice, y el número del subtítulo ("completaste
    # tu sesión número n") salía uno abajo — que además corre la cadencia de los
    # pedidos del resumen, porque se cuenta en sesiones.
    db.flush()

    # Racha global de días: completar cualquier sesión suma el día (una sola vez
    # por día). Más de STREAK_RESET_AFTER_DAYS días sin actividad resetean el
    # acumulado antes de contar el día de hoy.
    #
    # El día se gana con un UPDATE CONDICIONAL y no leyendo-sumando-escribiendo,
    # y no es cosmético: este resumen se pide por GET, así que dos pestañas, un
    # refetch de React o el prefetch del navegador pueden entrar a la vez. Con la
    # versión anterior las dos leían `streak_last_date != hoy` y las dos sumaban
    # +1 — y `streak_days` alimenta `streak_multiplier`, así que un día de más es
    # XP de más para siempre.
    #
    # El `WHERE` es lo que reparte: gana UNA sola transacción por día, y las demás
    # actualizan cero filas. `rowcount` es entonces la respuesta exacta a "¿este
    # request contó el día?", que es lo que el front usa para decidir si muestra
    # la pantalla de racha.
    user = db.query(User).filter(User.id == user_id).first()
    streak_counted_today = False
    if user:
        inactive_days = (
            (today - user.streak_last_date).days
            if user.streak_last_date is not None
            else 0
        )
        se_cortó = inactive_days > STREAK_RESET_AFTER_DAYS
        valores = {
            User.streak_last_date: today,
            # Expresión SQL, no un número calculado en Python: el incremento lo
            # hace la base sobre el valor que tenga la fila en ese momento.
            User.streak_days: 1 if se_cortó else User.streak_days + 1,
        }
        if se_cortó:
            # Quien pierde la racha y vuelve a llegar a un hito merece la
            # felicitación de nuevo (ver lifecycle_emails.due_streak_tier_emails).
            valores[User.streak_email_sent_tier] = None
        filas = (
            db.query(User)
            .filter(
                User.id == user_id,
                or_(User.streak_last_date.is_(None), User.streak_last_date != today),
            )
            .update(valores, synchronize_session=False)
        )
        streak_counted_today = filas == 1
        # El UPDATE no tocó el objeto en memoria: se expira para que lo de abajo
        # lea el valor que quedó en la base y no el de antes.
        db.expire(user, ["streak_days", "streak_last_date", "streak_email_sent_tier"])

    si = streak_info(user.streak_days if user else 0)

    # Nº de orden de esta sesión entre TODAS las sesiones terminadas por el
    # usuario (cualquier curso/modo), para el subtítulo "Completaste tu sesión
    # número n." del resumen.
    session_number = (
        db.query(SessionModel)
        .filter(
            SessionModel.user_id == user_id,
            SessionModel.finished_at.isnot(None),
            SessionModel.id <= db_session.id,
        )
        .count()
    )

    # ¿Le pedimos algo en esta pantalla? La regla entera vive en summary_asks —
    # acá solo se anota EN QUÉ sesión salió, que es lo que después mantiene al
    # café y al WhatsApp separados.
    #
    # La escritura es condicional para que un refetch del mismo resumen no
    # cuente como un pedido nuevo: `pedido_del_resumen` ya devuelve lo mismo
    # cuando el último pedido fue esta sesión, y sin la guarda commitearíamos en
    # cada visita a la misma pantalla.
    pedido = summary_asks.pedido_del_resumen(
        session_number=session_number,
        tier_reached=si.tier_reached,
        streak_days=(user.streak_days or 0) if user else 0,
        tiene_pwa=bool(user and user.pwa_first_seen_at is not None),
        tiene_handle=bool(user and user.username),
        ultimo_pedido=user.summary_ask_last_session if user else None,
    )
    if pedido is not None and user and user.summary_ask_last_session != session_number:
        user.summary_ask_last_session = session_number

    # UN solo commit para las tres cosas que este resumen escribe: el cierre de
    # la sesión, el día de racha y el pedido. Antes había dos, y en el hueco
    # entre ellos un request que se muere dejaba la sesión cerrada y la racha
    # contada pero el pedido sin anotar — o sea, el pedido saliendo dos veces
    # seguidas, que es exactamente lo que `summary_ask_last_session` existe para
    # impedir.
    db.commit()

    return {
        "session_id": str(session_id_db),
        "user_name": "",
        "mode": db_session.mode,
        "course": _get_course_slug(course_id, db),
        "total": total,
        "correct": correct_count,
        "first_try_correct": first_try_correct,
        "incorrect": incorrect_count,
        "items": items,
        "topic_states": topic_states,
        "xp_earned": xp_earned,
        "streak": {
            "days": si.days,
            "multiplier": si.multiplier,
            "next_threshold": si.next_threshold,
            "next_multiplier": si.next_multiplier,
            "days_to_next": si.days_to_next,
            "is_max": si.is_max,
            "tier_reached": si.tier_reached,
            "prev_multiplier": si.prev_multiplier,
            "counted_today": streak_counted_today,
            # Solo lo que puso la RACHA. El campo vive adentro de `streak` y se
            # dibuja al lado de su multiplicador, así que tiene que ser el número
            # de esa cuenta y no el del total.
            "xp_bonus": xp_bonus_racha,
        },
        # Y lo que puso el cafecito, aparte. Cero cuando no hubo empuje, que es
        # lo que hace que el front no tenga que ramificar para el caso normal.
        "xp_from_boost": xp_del_empuje,
        "pedido": pedido,
        "handle": user.username if user else None,
        "share_percent": referrals.SHARE_PERCENT,
        "session_number": session_number,
    }


# ── Editor de curso ──────────────────────────────────────────────────────────

def _course_total_items(course_id: int, db: DBSession) -> int:
    """Total de ítems (exercise_types) del curso: el máximo posible del cap."""
    types = course_exercise_types(course_id, db)
    return sum(
        len(types.get((k.belt.value, k.topic), []))
        for k in _all_topic_keys(course_id, db)
    )


def _topic_rows(
    user_id: int, course_id: int, belt: str, topic: str, db: DBSession,
) -> list[UnitState]:
    return db.query(UnitState).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
        UnitState.belt == belt,
        UnitState.topic == topic,
    ).all()


def _topic_key(course_id: int, belt: str, topic: str, db: DBSession) -> "TopicKey | None":
    for k in _all_topic_keys(course_id, db):
        if k.belt.value == belt and k.topic == topic:
            return k
    return None


def advance_topic(user_id: int, course_id: int, belt: str, topic: str, db: DBSession) -> None:
    """Adelantar: desbloquea un tema fuera de orden, o lo reactiva si estaba
    suspendido. Los ítems recién activados suben el contador de "ítems activos"
    (cap) si lo superan, así el contador refleja los ítems que agregaste."""
    rows = _topic_rows(user_id, course_id, belt, topic, db)
    if rows:
        for r in rows:
            r.suspended = False
    else:
        key = _topic_key(course_id, belt, topic, db)
        if key is None:
            raise ValueError(f"Tema desconocido: {belt}/{topic}")
        _create_topic_units(user_id, course_id, key, db)
    db.commit()

    # Subir el cap para incluir los ítems recién activados (adelantar es una
    # elección explícita de agregar ítems, aunque supere el cap actual).
    cp = _get_course_progress(user_id, course_id, db)
    active = _active_unit_count(user_id, course_id, db)
    if active > cp.active_cap:
        cp.active_cap = active
        db.commit()


def suspend_topic(user_id: int, course_id: int, belt: str, topic: str, db: DBSession) -> None:
    """Suspender: oculta el tema del home y lo excluye de sesiones/maestría. El
    cupo liberado se cede a los temas siguientes."""
    rows = _topic_rows(user_id, course_id, belt, topic, db)
    for r in rows:
        r.suspended = True
    db.commit()
    _ensure_active_units(user_id, course_id, db)


def reset_topic(user_id: int, course_id: int, belt: str, topic: str, db: DBSession) -> None:
    """Reiniciar tema: todos sus ítems vuelven a 'nuevo' (learning fresco, vencen
    hoy) y reingresan como candidatos de repaso."""
    rows = _topic_rows(user_id, course_id, belt, topic, db)
    today = user_today(db, user_id)
    for r in rows:
        r.phase = "learning"
        r.step_index = 0
        r.ease_factor = 2.5
        r.interval_days = 1
        r.repetitions = 0
        r.next_due = today
        r.attempted = False
        r.suspended = False
        r.last_reviewed_at = None
        r.updated_at = datetime.utcnow()
    db.commit()


def _relock_last_items(user_id: int, course_id: int, cap: int, db: DBSession) -> list[str]:
    """Suspende ítems en aprendizaje desde el final del orden de catálogo hasta
    que la cantidad activa no supere `cap`. Devuelve 'belt/topic' de los
    tocados.

    Usa suspensión (mismo mecanismo que `suspend_topic`), no borrado: un ítem
    en aprendizaje puede tener intentos reales (`attempted`, `step_index`
    avanzado), y borrarlo perdía ese progreso para siempre. Suspendido, el
    progreso queda intacto y recuperable con "Adelantar" (`advance_topic`) si
    el usuario vuelve a subir el cap.

    Las filas huérfanas (exercise_type podado del catálogo) no cuentan para
    `active` (ver `_active_unit_count`), así que tampoco se tocan acá: si se
    suspendieran igual, `active` bajaría por una unit que nunca estuvo
    ocupando cupo real, y el loop se frenaría un tema antes de lo que
    corresponde."""
    keys = _all_topic_keys(course_id, db)
    current_types = _current_exercise_types(course_id, db)
    active = _active_unit_count(user_id, course_id, db, current=current_types)
    touched: list[str] = []
    for tk in reversed(keys):
        if active <= cap:
            break
        rows = _topic_rows(user_id, course_id, tk.belt.value, tk.topic, db)
        learning = [
            r for r in rows
            if r.phase != "review" and not r.suspended
            and (r.belt, r.topic, r.exercise_type) in current_types
        ]
        if not learning:
            continue
        removed = False
        for r in learning:
            if active <= cap:
                break
            r.suspended = True
            active -= 1
            removed = True
        if removed:
            touched.append(f"{tk.belt.value}/{tk.topic}")
    db.commit()
    return touched


def set_active_cap(user_id: int, course_id: int, value: int, db: DBSession) -> int:
    """Fija cuántos ítems puede tener en aprendizaje (clamp 1..total). Subir
    desbloquea más; bajar re-bloquea los últimos ítems en aprendizaje."""
    total = _course_total_items(course_id, db)
    value = max(1, min(int(value), total))
    cp = _get_course_progress(user_id, course_id, db)
    old = cp.active_cap
    cp.active_cap = value
    db.commit()
    if value > old:
        _ensure_active_units(user_id, course_id, db)
    elif value < old:
        _relock_last_items(user_id, course_id, value, db)
    return value


def set_session_size(user_id: int, course_id: int, value: int, db: DBSession) -> int:
    """Fija el máximo de ejercicios por sesión (clamp SESSION_SIZE_MIN..MAX).
    Apaga la rampa automática: a partir de acá manda el valor manual hasta que
    se reinicie el curso (ver course_progress.session_size_auto)."""
    value = max(SESSION_SIZE_MIN, min(int(value), SESSION_SIZE_MAX))
    cp = _get_course_progress(user_id, course_id, db)
    cp.session_size = value
    cp.session_size_auto = False
    db.commit()
    return value


def cap_change_preview(user_id: int, course_id: int, value: int, db: DBSession) -> dict:
    """Sin aplicar: qué temas se desbloquean/re-bloquean al cambiar el cap."""
    types = course_exercise_types(course_id, db)
    keys = _all_topic_keys(course_id, db)
    total = sum(len(types.get((k.belt.value, k.topic), [])) for k in keys)
    value = max(1, min(int(value), total))
    current_types = _current_exercise_types(course_id, db, types=types)
    active = _active_unit_count(user_id, course_id, db, current=current_types)
    unlock: list[str] = []
    lock: list[str] = []
    if value > active:
        remaining = value - active
        seen_topics = _topics_with_units(
            user_id, course_id, db, current=current_types
        )
        for tk in keys:
            if remaining <= 0:
                break
            if (tk.belt.value, tk.topic) in seen_topics:
                continue
            n = len(types.get((tk.belt.value, tk.topic), []))
            if n > remaining:
                break
            unlock.append(f"{tk.belt.value}/{tk.topic}")
            remaining -= n
    elif value < active:
        remaining = active - value
        for tk in reversed(keys):
            if remaining <= 0:
                break
            rows = _topic_rows(user_id, course_id, tk.belt.value, tk.topic, db)
            learning = [
                r for r in rows
                if r.phase != "review" and not r.suspended
                and (r.belt, r.topic, r.exercise_type) in current_types
            ]
            if not learning:
                continue
            lock.append(f"{tk.belt.value}/{tk.topic}")
            remaining -= min(len(learning), remaining)
    return {"value": value, "unlock": unlock, "lock": lock}


def reset_course(user_id: int, course_id: int, db: DBSession) -> int:
    """Reiniciar curso: archiva el progreso vigente en unit_state_archive con la
    iteración actual, lo borra de unit_states e incrementa la iteración. Los
    Answer/Session quedan etiquetados con su iteración. Devuelve la nueva."""
    cp = _get_course_progress(user_id, course_id, db)
    rows = db.query(UnitState).filter(
        UnitState.user_id == user_id,
        UnitState.course_id == course_id,
    ).all()
    for r in rows:
        db.add(UnitStateArchive(
            user_id=user_id, course_id=course_id, iteration=cp.iteration,
            belt=r.belt, topic=r.topic, exercise_type=r.exercise_type,
            phase=r.phase, step_index=r.step_index, ease_factor=r.ease_factor,
            interval_days=r.interval_days, repetitions=r.repetitions,
            next_due=r.next_due, attempted=r.attempted,
            is_catchup=r.is_catchup, suspended=r.suspended,
        ))
        db.delete(r)
    db.query(ItemExerciseCycle).filter(
        ItemExerciseCycle.user_id == user_id,
        ItemExerciseCycle.course_id == course_id,
    ).delete()
    cp.iteration += 1
    slug = _get_course_slug(course_id, db)
    cp.active_cap = ACTIVE_CAP_DEFAULTS.get(slug, ACTIVE_CAP_DEFAULT_FALLBACK)
    # El máximo de ejercicios por sesión también vuelve al default y a la
    # rampa automática: si el usuario lo había subido a mano para acelerar y
    # ahora reinicia el curso, arranca de nuevo con el ritmo pensado para
    # alguien que empieza.
    cp.session_size = SESSION_SIZE_DEFAULT
    cp.session_size_auto = True
    db.commit()
    return cp.iteration


# ── Abandono de sesiones ─────────────────────────────────────────────────────────

# Una sesión sin finished_at puede estar en curso o abandonada; la única señal
# disponible es el tiempo transcurrido. 2h es holgado: la sesión más larga
# observada dura minutos, así que nada legítimo sigue abierto pasado ese plazo.
ABANDON_AFTER_HOURS = 2


def sweep_abandoned_sessions(db: DBSession, older_than_hours: int = ABANDON_AFTER_HOURS) -> int:
    """Marcar como abandonadas las sesiones que quedaron sin terminar.

    `Session.abandoned` no se puede escribir en el momento en que ocurre el
    abandono — nadie avisa que se fue. Se deriva del tiempo: sin `finished_at`
    y empezada hace más de `older_than_hours`. Idempotente (solo toca filas con
    `abandoned` en false), así que puede correr en loop sin efectos acumulados.

    Devuelve cuántas filas se marcaron.
    """
    cutoff = datetime.utcnow() - timedelta(hours=older_than_hours)
    updated = (
        db.query(SessionModel)
        .filter(
            SessionModel.finished_at.is_(None),
            # isnot(True), no is_(False): la columna es nullable y las filas
            # viejas tienen NULL, que is_(False) no matchea.
            SessionModel.abandoned.isnot(True),
            SessionModel.started_at < cutoff,
        )
        .update({SessionModel.abandoned: True}, synchronize_session=False)
    )
    db.commit()
    return updated
