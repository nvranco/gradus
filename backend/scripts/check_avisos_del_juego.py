"""Verifica el canal de avisos del minijuego: cupo, horario, copy e invitados.

El canal de Intervalo no le llegaba al juego por tres cortes —`push_subscriptions`
pide un usuario, `due_notifications` corta en repasos SM-2 pendientes, y todo el
contexto sale de tablas que el juego no toca—, así que el juego tiene el suyo.
Lo que se prueba acá es lo que no se ve mirando el teléfono:

· Que un INVITADO reciba. Es la mitad de la razón de que este canal exista: entre
  el 50% y el 95% de cada cohorte del juego no tiene cuenta.
· Que el cupo sea de la PERSONA y no del producto. Un jugador registrado reclama
  contra los contadores de su usuario, así que tres avisos de dx dejan a
  Intervalo sin nada ese día. Es lo que puede romper el canal que hoy funciona.
· Que registrarse no apague los recordatorios que se prendieron de invitado.
· Que la escalera de reactivación TERMINE. Sale a los días 1, 3, 7 y 14 y después
  nunca más; al mes se apaga el canal solo.
· Que el copy elija por el hecho, y que lo de universidades no invente un número
  de XP: ese ranking va por Elo promedio y decir «está a N XP» sería un número
  que la propia tabla del juego desmiente.
· Que el payload valide contra el schema POR HTTP. Este check existe en parte
  porque una vez se shippeó un 500 llamando al store directo y salteando FastAPI.

Uso:
    python backend/scripts/check_avisos_del_juego.py

Sale con código 1 si algo falla.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = "sqlite:///" + str(
    Path(tempfile.mkdtemp()) / "avisos_juego.db"
).replace("\\", "/")
os.environ.setdefault("INTERNAL_API_SECRET", "secreto-de-prueba")
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent))

import database  # noqa: E402
from models import (  # noqa: E402
    Base,
    Course,
    GameAttempt,
    GameBoost,
    GameEvent,
    GameNotificationSend,
    GamePlayer,
    GamePushSubscription,
    User,
)

Base.metadata.create_all(database.engine)

import push_store  # noqa: E402
from game import boosts  # noqa: E402
from game import notification_copy as copy  # noqa: E402
from game import notifications as avisos  # noqa: E402

fallos: list[str] = []


def check(nombre: str, cond: bool, detalle: str = "") -> None:
    print(f"{'ok   ' if cond else 'FALLA'}  {nombre} {detalle}".rstrip())
    if not cond:
        fallos.append(nombre)


TZ = "America/Argentina/Buenos_Aires"
s = database.SessionLocal()

s.add(Course(id=1, slug="analisis-1", name="Análisis 1"))

# p1  INVITADO con avisos prendidos a las 09:00. Es el caso que este canal existe
#     para cubrir: no tiene fila en `users` y hoy no recibe nada.
# p2  REGISTRADO. Su cupo es el del usuario, compartido con Intervalo.
# p3  invitado sin suscribir: no puede recibir aunque tenga todo lo demás.
s.add(
    User(
        id=2,
        clerk_user_id="u2",
        email="dos@x.com",
        name="Dos",
        notify_enabled=True,
        notify_time="09:00",
        notify_timezone=TZ,
    )
)
s.flush()

# Naive UTC, que es como guardan las columnas. Sin `utcnow()`, que está
# deprecada y ensucia la salida del runner con un warning por corrida.
AHORA = datetime.now(tz=ZoneInfo("UTC")).replace(tzinfo=None)
JUGADORES = [
    dict(id=1, user_id=None, alias="uno", university="UBA", notify_enabled=True,
         notify_time="09:00", notify_timezone=TZ),
    dict(id=2, user_id=2, alias="dos", university="UBA"),
    dict(id=3, user_id=None, alias="tres", university="UTN", notify_enabled=True,
         notify_time="09:00", notify_timezone=TZ),
]
for j in JUGADORES:
    s.add(
        GamePlayer(
            theta=0.5, n_updates=20, xp=100, exercises_correct=10, best_combo=7,
            unlocked_keys="pow", is_bot=False, created_at=AHORA - timedelta(days=3),
            last_seen_at=AHORA, **j
        )
    )
s.flush()

for pid in (1, 2):
    s.add(
        GamePushSubscription(
            player_id=pid, endpoint=f"https://push.test/{pid}", p256dh="k", auth="a"
        )
    )
s.commit()

p1 = s.get(GamePlayer, 1)
p2 = s.get(GamePlayer, 2)
p3 = s.get(GamePlayer, 3)
u2 = s.get(User, 2)


def limpiar_envios() -> None:
    s.query(GameNotificationSend).delete()
    for p in (p1, p2, p3):
        p.notify_last_sent_on = None
        p.notify_events_on = None
        p.notify_events_count = 0
    u2.notify_last_sent_on = None
    u2.notify_events_on = None
    u2.notify_events_count = 0
    s.commit()


# ── 1 · El titular del cupo ─────────────────────────────────────────────────
print("\n— de quién es el cupo —")
check("el invitado es su propio titular", avisos.titular_del_cupo(p1) is p1)
check("el registrado usa su usuario", avisos.titular_del_cupo(p2) is u2)
# Y por eso las preferencias del registrado son las de Intervalo: la misma
# persona no puede tener dos horarios.
check(
    "y las preferencias del registrado salen de users",
    avisos.get_settings(p2) == {"enabled": True, "time": "09:00", "timezone": TZ},
    f"({avisos.get_settings(p2)})",
)

# ── 2 · Un invitado recibe ──────────────────────────────────────────────────
print("\n— el invitado —")
limpiar_envios()
salida = avisos.due_game_notifications(s, force=True)
para = {x["player_id"] for x in salida}
check("un invitado sin cuenta recibe su aviso", 1 in para, f"({sorted(para)})")
check("y el que no suscribió ningún navegador no", 3 not in para)
check(
    "el aviso lleva a dónde abrir",
    all(x["url"] == "/derivadas" for x in salida),
    f"({[x['url'] for x in salida]})",
)
check(
    "y viaja con la suscripción del navegador",
    all(x["subscriptions"] and x["subscriptions"][0]["endpoint"] for x in salida),
)

# ── 3 · Un aviso por día, reclamado al decidirlo ────────────────────────────
print("\n— el cupo programado —")
limpiar_envios()
uno = avisos.due_game_notifications(s)
# Sin `force` el horario tiene que coincidir, así que puede no salir ninguno:
# lo que importa es que dos corridas seguidas nunca den dos avisos a la misma
# persona.
dos = avisos.due_game_notifications(s)
check(
    "dos corridas seguidas no mandan dos veces",
    not ({x["player_id"] for x in uno} & {x["player_id"] for x in dos}),
)
limpiar_envios()
avisos.due_game_notifications(s, force=True)
check("el día queda marcado en el titular", p1.notify_last_sent_on is not None)

# ── 4 · El cupo es de la PERSONA, no del producto ───────────────────────────
print("\n— dx primero, y el cupo compartido —")
limpiar_envios()
hoy_local = datetime.now(tz=ZoneInfo(TZ)).date()
# El jugador registrado consume sus dos cupos de evento. Como el titular es el
# usuario, Intervalo se queda sin ninguno ese día: es exactamente el orden que
# se buscó, y lo que se rompería en silencio si `titular_del_cupo` devolviera el
# jugador.
check("le quedan dos avisos reactivos", push_store.claim_event_slot(s, u2, hoy_local))
check("y el segundo también", push_store.claim_event_slot(s, u2, hoy_local))
check("el tercero no", not push_store.claim_event_slot(s, u2, hoy_local))
s.commit()
check(
    "y el contador quedó en el USUARIO, que es lo que mira Intervalo",
    u2.notify_events_count == 2 and (p2.notify_events_count or 0) == 0,
    f"(usuario {u2.notify_events_count}, jugador {p2.notify_events_count})",
)
check(
    "el invitado tiene el suyo, aparte",
    push_store.claim_event_slot(s, p1, hoy_local) and p1.notify_events_count == 1,
)
s.commit()

# ── 5 · Registrarse no apaga los recordatorios ──────────────────────────────
print("\n— al registrarse —")
u_nuevo = User(id=9, clerk_user_id="u9", email="nueve@x.com", name="Nueve")
s.add(u_nuevo)
s.flush()
avisos.copiar_preferencias_al_usuario(p1, u_nuevo)
check(
    "lo que eligió de invitado se muda a la cuenta",
    u_nuevo.notify_enabled and u_nuevo.notify_time == "09:00" and u_nuevo.notify_timezone == TZ,
    f"({u_nuevo.notify_enabled}, {u_nuevo.notify_time})",
)
# Pero no pisa a quien ya venía usando Intervalo: allá eligió su horario.
antes = u2.notify_time
avisos.copiar_preferencias_al_usuario(p1, u2)
check("y no le pisa el horario a quien ya tenía uno", u2.notify_time == antes)
s.rollback()

# ── 6 · La escalera de reactivación termina ────────────────────────────────
print("\n— la reactivación —")
sale = [
    d
    for d in range(0, 35)
    if copy.elegir_reactiva  # no aplica, es programada: se evalúa la variante
    and any(v.disponible({"dias_inactivo": d}) for v in copy.VARIANTES[copy.CAT_REACTIVACION])
]
check(
    "sale a los días 1, 3, 7 y 14 y en ninguno más",
    sale == list(copy.DIAS_DE_REACTIVACION),
    f"({sale})",
)
limpiar_envios()
p1.last_seen_at = AHORA - timedelta(days=copy.DIAS_PARA_APAGAR + 1)
s.commit()
avisos.due_game_notifications(s, force=True)
check(
    "y al mes de silencio el canal se apaga solo",
    not p1.notify_enabled,
    f"(notify_enabled={p1.notify_enabled})",
)
p1.notify_enabled = True
p1.last_seen_at = AHORA
s.commit()

# ── 7 · El copy elige por el hecho ─────────────────────────────────────────
print("\n— el copy —")
_, cuerpo = copy.VARIANTES[copy.CAT_SOCIAL][0].render({"universidad": "UBA", "companeros_hoy": 9})
check("social nombra la universidad y el número", "9 compañeros de la UBA" in cuerpo, cuerpo)
check(
    "y con pocos compañeros no sale",
    not copy.VARIANTES[copy.CAT_SOCIAL][0].disponible(
        {"universidad": "UBA", "companeros_hoy": copy.MIN_COMPANEROS}
    ),
)
ctx = {"universidad": "UBA", "donante": "Nico", "empuje_mult": 1.4, "empuje_horas": 24,
       "jugo_hoy": True}
v = copy.elegir_reactiva(copy.CAT_EMPUJE, ctx)
check("el cafecito nombra al donante sin arroba", v.key == "empuje_nombrado")
check("y escribe el multiplicador con coma", "×1,4" in v.render(ctx)[1], v.render(ctx)[1])
ctx_termina = dict(ctx, jugo_hoy=False, empuje_horas=3)
check(
    "si se termina y no jugó hoy, gana ese",
    copy.elegir_reactiva(copy.CAT_EMPUJE, ctx_termina).key == "empuje_termina",
)
check(
    "pero si ya jugó hoy no se le cuenta lo que está viendo",
    copy.elegir_reactiva(copy.CAT_EMPUJE, dict(ctx_termina, jugo_hoy=True)).key
    != "empuje_termina",
)
check(
    "sin universidad el cafecito es el global",
    copy.elegir_reactiva(copy.CAT_EMPUJE, {"empuje_mult": 1.2, "empuje_horas": 24}).key
    == "empuje_global",
)
check(
    "sin contexto no se inventa un aviso",
    copy.elegir_programada(contexto={}, ultima_categoria=None, ultima_variante=None) is None,
)
check("los pesos suman 1", abs(sum(copy.PESOS.values()) - 1.0) < 1e-9)

# EL número que no puede aparecer. El ranking de universidades del juego va por
# Elo promedio —está escrito así en game_university_leaderboard, y es lo que
# impide que un cafecito compre un puesto— así que un aviso que diga «te faltan
# N XP» manda a la gente a hacer lo único que NO mueve esa tabla.
_, uni_cerca = copy.VARIANTES[copy.CAT_UNIVERSIDAD][1].render(
    {"universidad": "UBA", "rival_universidad": "UTN"}
)
check("lo de universidades no promete XP", "XP" not in uni_cerca, uni_cerca)
todos = [
    v.render(
        {
            "universidad": "UBA", "rival_universidad": "UTN", "companeros_hoy": 9,
            "xp_universidad": 300, "xp_propia": 40, "dias_inactivo": 3,
            "mejor_tanda": 7, "empuje_mult": 1.4, "empuje_horas": 3, "jugo_hoy": False,
            "donante": "Nico", "recluta_alias": "ana", "recluta_xp": 30, "reclutas": 1,
            "primer_recluta": True, "rival_alias": "ana", "perdio_puesto": True,
            "uni_paso": True, "uni_cerca": True,
        }
    )
    for lista in copy.VARIANTES.values()
    for v in lista
]
check("todas las variantes se titulan dx", all(t == "dx" for t, _ in todos))
check("y ninguna queda con un placeholder sin llenar", not any("{" in c for _, c in todos))

# ── 8 · Reactivos de verdad, sobre la base ─────────────────────────────────
print("\n— la tubería reactiva —")
limpiar_envios()
boosts.olvidar_cache_de_empujes()
s.add(
    GameBoost(
        university="UBA", cafecitos=4, donor_name="Nico", source="cafecito",
        created_at=AHORA, expires_at=AHORA + timedelta(hours=24),
    )
)
s.commit()
eventos = avisos.due_game_event_notifications(s, force=True)
de_uba = [e for e in eventos if e["player_id"] in (1, 2)]
check("el cafecito de la UBA avisa a sus jugadores", len(de_uba) == 2, f"({len(de_uba)})")
check("y el cuerpo dice el multiplicador", all("×" in e["body"] for e in de_uba))
otra_vez = avisos.due_game_event_notifications(s, force=True)
check(
    "el mismo cafecito no vuelve a avisar hoy",
    not [e for e in otra_vez if e["player_id"] in (1, 2)],
    f"({len(otra_vez)})",
)

# ── 9 · El payload, por HTTP ───────────────────────────────────────────────
print("\n— el contrato —")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


def _sesion():
    try:
        yield s
    finally:
        pass


main.app.dependency_overrides[main.get_db] = _sesion
cliente = TestClient(main.app)
cab = {"X-Internal-Secret": os.environ["INTERNAL_API_SECRET"]}

limpiar_envios()
r = cliente.get("/internal/notifications/game?force=true", headers=cab)
check("el endpoint programado responde 200", r.status_code == 200, f"({r.status_code})")
check("y devuelve avisos", len(r.json()) >= 1, f"({len(r.json())})")
r2 = cliente.get("/internal/notifications/game-events?force=true", headers=cab)
check("el reactivo también", r2.status_code == 200, f"({r2.status_code})")
check("sin secreto no se entra", cliente.get("/internal/notifications/game").status_code == 401)

# El reporte de entrega y el prune tienen que apuntar a las tablas del JUEGO: los
# dos espacios de ids se solapan, así que mandarlo al endpoint de Intervalo
# marcaría la fila de otra persona.
envio_id = r.json()[0]["notification_id"]
sub_id = r.json()[0]["subscriptions"][0]["id"]
cliente.post(
    "/internal/push/game-delivery",
    headers=cab,
    json={"results": [{"notification_id": envio_id, "status": "ok"}]},
)
s.expire_all()
check(
    "la entrega se anota en la fila del juego",
    s.get(GameNotificationSend, envio_id).delivery_status == "ok",
)
cliente.post("/internal/push/game-prune", headers=cab, json={"subscription_ids": [sub_id]})
s.expire_all()
check("y el prune borra la suscripción muerta", s.get(GamePushSubscription, sub_id) is None)

print()
if fallos:
    print(f"{len(fallos)} fallo(s): " + ", ".join(fallos))
    sys.exit(1)
print("todo ok")
