"""Verifica el empuje automático por aforo (game/aforo.py).

Lo que se defiende acá:
  - se cuentan PERSONAS y no filas: quien juega de invitado y después se
    registra el mismo día vale uno, no dos;
  - la persona número 10 lo prende y la novena no;
  - una sola vez por universidad y por día, aunque entren veinte más — y
    tampoco lo prende dos veces una carrera entre dos altas simultáneas;
  - al día siguiente vuelve a estar disponible;
  - el día es el argentino, no el UTC (a las 22:30 de Buenos Aires sigue
    siendo hoy aunque en UTC ya sea mañana);
  - el multiplicador es ×1,5 y dura 2 h, y SUMA con un cafecito vigente;
  - el cartel no miente: `cafecitos` cuenta solo lo donado y el empuje de
    aforo se anuncia con su propia bandera;
  - el feed no dice "invitó cafecitos" de algo que no invitó nadie;
  - el mail de agradecimiento no le agradece a nadie por un empuje de aforo,
    ni siquiera si hay una intención consumida en la misma ventana de ±5 s;
  - `revisar` no puede voltear un alta: si algo explota adentro, devuelve None.

Uso:
    python backend/scripts/check_aforo.py

Sale con código 1 si algo falla.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = "sqlite:///" + str(
    Path(tempfile.mkdtemp()) / "aforo.db"
).replace("\\", "/")
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent))

import database  # noqa: E402
from models import (  # noqa: E402
    Base,
    Course,
    Enrollment,
    GameBoost,
    GameEvent,
    GamePlayer,
    User,
)
from game import aforo, boosts, events  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  [{'ok' if cond else 'FALLA'}] {label}")
    if not cond:
        FAILURES.append(label)


Base.metadata.create_all(bind=database.engine)
db = database.SessionLocal()
db.add(Course(id=1, name="Análisis", slug="analisis"))
db.commit()

# Un mediodía argentino cualquiera, para que el día local y el UTC coincidan y
# los casos de borde se prueben aparte y a propósito.
HOY = datetime(2026, 9, 8, 15, 0, 0)  # 12:00 en Buenos Aires

_seq = 0


def alta_clasico(uni: str, cuando: datetime) -> User:
    """Un alta de Intervalo clásico: usuario + enrollment con universidad."""
    global _seq
    _seq += 1
    u = User(email=f"u{_seq}@x.com", name=f"U{_seq}", username=f"u{_seq}", created_at=cuando)
    db.add(u)
    db.flush()
    db.add(Enrollment(user_id=u.id, course_id=1, university=uni))
    db.flush()
    return u


def jugador(uni: str | None, cuando: datetime, user_id: int | None = None,
            is_bot: bool = False) -> GamePlayer:
    global _seq
    _seq += 1
    p = GamePlayer(guest_token=f"g{_seq}", alias=f"j{_seq}", university=uni,
                   created_at=cuando, user_id=user_id, is_bot=is_bot)
    db.add(p)
    db.flush()
    return p


print("1. se cuentan personas, no filas")
for _ in range(4):
    alta_clasico("UBA", HOY)
for _ in range(3):
    jugador("UBA", HOY)
db.commit()
check(aforo.personas_nuevas_hoy(db, "UBA", HOY) == 7,
      f"4 altas + 3 invitados = 7 (dio {aforo.personas_nuevas_hoy(db, 'UBA', HOY)})")

# El caso que hace que esto no sea una suma boba: el que jugó de invitado y se
# registró el mismo día deja DOS filas y es UNA persona.
u = alta_clasico("UBA", HOY)
jugador("UBA", HOY, user_id=u.id)
db.commit()
check(aforo.personas_nuevas_hoy(db, "UBA", HOY) == 8,
      f"el invitado que se registra sigue siendo uno (dio {aforo.personas_nuevas_hoy(db, 'UBA', HOY)})")

jugador("UBA", HOY, is_bot=True)
jugador(None, HOY)
jugador("UTN", HOY)
alta_clasico("UTN", HOY)
db.commit()
check(aforo.personas_nuevas_hoy(db, "UBA", HOY) == 8, "ni los bots, ni los sin universidad, ni los de otra")
check(aforo.personas_nuevas_hoy(db, "UTN", HOY) == 2, "y la otra universidad cuenta lo suyo")

# Los de ayer no cuentan hoy.
alta_clasico("UBA", HOY - timedelta(days=1))
db.commit()
check(aforo.personas_nuevas_hoy(db, "UBA", HOY) == 8, "un alta de ayer no cuenta hoy")


print("\n2. la novena no lo prende, la décima sí")
check(aforo.revisar(db, "UBA", HOY) is None, f"con 8 no pasa nada")
alta_clasico("UBA", HOY)
db.commit()
check(aforo.personas_nuevas_hoy(db, "UBA", HOY) == 9, "novena")
check(aforo.revisar(db, "UBA", HOY) is None, "la novena tampoco")

alta_clasico("UBA", HOY)
db.commit()
boost = aforo.revisar(db, "UBA", HOY)
db.commit()
check(boost is not None, "la décima lo prende")
check(boost is not None and boost.source == aforo.SOURCE, "queda sellado como aforo")
check(boost is not None and boost.donor_name is None, "y sin donante, porque no lo hubo")


print("\n3. ×1,5 durante 2 horas")
check(abs(boosts.multiplier_for(db, "UBA", now=HOY) - 1.5) < 1e-9,
      f"×1,5 (dio {boosts.multiplier_for(db, 'UBA', now=HOY)})")
dura = (boost.expires_at - boost.created_at).total_seconds() / 3600
check(abs(dura - 2.0) < 1e-9, f"dura 2 h (dio {dura})")
check(abs(boosts.multiplier_for(db, "UBA", now=HOY + timedelta(hours=2, minutes=1)) - 1.0) < 1e-9,
      "a las 2 h 1 min ya no vale")
check(abs(boosts.multiplier_for(db, "UTN", now=HOY) - 1.0) < 1e-9,
      "y no se lo lleva la universidad de al lado")


print("\n4. una sola vez por día, incluso con la persona 11 y con carreras")
for _ in range(10):
    alta_clasico("UBA", HOY)
db.commit()
check(aforo.personas_nuevas_hoy(db, "UBA", HOY) == 20, "ahora son 20")
check(aforo.revisar(db, "UBA", HOY) is None, "la 11 (y la 20) no lo vuelven a prender")
check(db.query(GameBoost).filter(GameBoost.source == aforo.SOURCE).count() == 1,
      "sigue habiendo un solo empuje de aforo")

# La carrera: dos altas simultáneas que pasan el `ya_se_dio_hoy` a la vez. Se
# simula saltándose esa guarda y yendo derecho al grant, que es lo que hace el
# UNIQUE de external_ref.
repetido = boosts.grant(
    db, university="UBA", cafecitos=aforo.CAFECITOS_EQUIVALENTES,
    source=aforo.SOURCE, external_ref=aforo.referencia_de("UBA", HOY),
    minutes=aforo.BOOST_MINUTOS, now=HOY, anunciar=False,
)
check(repetido is None, "el UNIQUE de external_ref frena la carrera sin candado")
check(abs(boosts.multiplier_for(db, "UBA", now=HOY) - 1.5) < 1e-9, "y el multiplicador no se duplicó")


print("\n5. mañana vuelve a estar disponible")
MANANA = HOY + timedelta(days=1)
for _ in range(10):
    alta_clasico("UBA", MANANA)
db.commit()
check(aforo.personas_nuevas_hoy(db, "UBA", MANANA) == 10, "el conteo arranca de cero mañana")
otro = aforo.revisar(db, "UBA", MANANA)
db.commit()
check(otro is not None, "y se puede volver a ganar")
check(otro is not None and otro.external_ref != boost.external_ref, "con otra referencia")


print("\n6. el día es el argentino, no el UTC")
# 2026-09-10 01:30 UTC son las 22:30 del 9 en Buenos Aires: sigue siendo el 9.
TARDE_UTC = datetime(2026, 9, 10, 1, 30, 0)
check(aforo.dia_de(TARDE_UTC).isoformat() == "2026-09-09",
      f"22:30 de Buenos Aires todavía es el 9 (dio {aforo.dia_de(TARDE_UTC)})")
for _ in range(10):
    alta_clasico("UNC", TARDE_UTC - timedelta(hours=6))  # 16:30 del 9, mismo día local
db.commit()
check(aforo.personas_nuevas_hoy(db, "UNC", TARDE_UTC) == 10,
      "las altas de la tarde cuentan para la noche del mismo día local")
check(aforo.referencia_de("UNC", TARDE_UTC).endswith("2026-09-09"), "y la referencia usa el día local")


print("\n7. suma con un cafecito vigente, sin pisarlo")
DIA3 = datetime(2026, 9, 11, 15, 0, 0)
boosts.grant(db, university="UNLP", cafecitos=3, donor_name="Mati",
             source="manual", external_ref="test-mati", now=DIA3)
db.commit()
check(abs(boosts.multiplier_for(db, "UNLP", now=DIA3) - 1.3) < 1e-9, "el cafecito solo da ×1,3")
for _ in range(10):
    alta_clasico("UNLP", DIA3)
db.commit()
aforo.revisar(db, "UNLP", DIA3)
db.commit()
check(abs(boosts.multiplier_for(db, "UNLP", now=DIA3) - 1.8) < 1e-9,
      f"con el aforo encima queda ×1,8 (dio {boosts.multiplier_for(db, 'UNLP', now=DIA3)})")

vista = [v for v in boosts.active_boosts(db, now=DIA3) if v.university == "UNLP"][0]
check(vista.cafecitos == 3, f"el cartel cuenta 3 cafecitos, los que se donaron (dio {vista.cafecitos})")
check(vista.aforo is True, "y avisa que además hubo aforo")
check(vista.donor_name == "Mati", "sin perder el nombre del que sí donó")

solo_aforo = [v for v in boosts.active_boosts(db, now=MANANA) if v.university == "UBA"][0]
check(solo_aforo.cafecitos == 0 and solo_aforo.aforo is True,
      f"un empuje de aforo puro va con 0 cafecitos y la bandera (dio {solo_aforo.cafecitos})")


print("\n8. el feed dice la verdad")
# El nombre del donante NO está en `text` —ahí va el marcador `{a}`, que el
# cliente reemplaza— así que hay que mirar `actor_alias`.
eventos = db.query(GameEvent).filter(GameEvent.kind == "boost").all()
invito = [e for e in eventos if "invitó" in e.text]
n_aforo = db.query(GameBoost).filter(GameBoost.source == aforo.SOURCE).count()
check(len(invito) == 1 and invito[0].actor_alias == "Mati",
      f"un solo evento de donación, y es el de Mati (dio {[(e.actor_alias, e.text[:30]) for e in invito]})")
textos = [e.text for e in eventos]
aforos = [t for t in textos if "personas nuevas" in t]
check(len(aforos) == n_aforo,
      f"un evento de aforo por cada empuje de aforo ({len(aforos)} eventos, {n_aforo} empujes)")
check(all(e.actor_alias is None for e in eventos if "personas nuevas" in e.text),
      "y ninguno le pone nombre de protagonista a algo que hicieron diez")
check(any("×1,5" in t for t in aforos), "con el multiplicador en la frase")
check(any("2 horas" in t for t in aforos), "y con la duración")


print("\n9. el mail de agradecimiento no le agradece a nadie por el aforo")
from models import GameBoostIntent  # noqa: E402
import lifecycle_emails  # noqa: E402

# El caso peligroso: una intención consumida justo en la ventana de ±5 s del
# empuje automático. Sin el filtro por source, el mail le agradecería a esta
# persona un empuje que se ganó la universidad entera.
u_donante = alta_clasico("UNSAM", DIA3)
p_donante = jugador("UNSAM", DIA3, user_id=u_donante.id)
db.commit()
for _ in range(10):
    alta_clasico("UNSAM", DIA3)
db.commit()
b_aforo = aforo.revisar(db, "UNSAM", DIA3)
db.add(GameBoostIntent(player_id=p_donante.id, university="UNSAM",
                       created_at=DIA3, consumed_at=b_aforo.created_at))
db.commit()

# Se vence, que es cuando el worker lo mira.
b_aforo.expires_at = datetime.utcnow() - timedelta(minutes=1)
db.commit()
salida = lifecycle_emails.due_cafecito_efecto_emails(db)
destinatarios = [u.email for u, _ in salida]
check(u_donante.email not in destinatarios,
      f"nadie recibe el mail por un empuje de aforo (destinatarios: {destinatarios})")


print("\n10. revisar() no puede voltear un alta")
check(aforo.revisar(db, None, HOY) is None, "sin universidad devuelve None")
check(aforo.revisar(db, "  ", HOY) is None, "con una universidad vacía también")


class SesionRota:
    def query(self, *a, **k):
        raise RuntimeError("la base se cayó")


check(aforo.revisar(SesionRota(), "UBA", HOY) is None,
      "y si la base falla devuelve None en vez de tirar el alta abajo")


print("\n11. las dos constantes de `source` dicen lo mismo")
check(boosts._SOURCE_AFORO == aforo.SOURCE,
      f"boosts._SOURCE_AFORO ({boosts._SOURCE_AFORO}) == aforo.SOURCE ({aforo.SOURCE})")
check(aforo.CAFECITOS_EQUIVALENTES * boosts.CAFECITO_STEP + 1.0 == aforo.MULTIPLICADOR,
      "y los cafecitos equivalentes dan exactamente el multiplicador prometido")


print()
print("12. los DOS enganches, por HTTP y no llamando a revisar() a mano")
# Lo que esta sección defiende es el cableado, no la función: `revisar` puede
# estar perfecta y no estar colgada de ningún endpoint. Es el hueco que la
# auditoría del cruce encontró dos veces (checks que pasan por debajo de la
# capa donde está el bug).
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

# Los dos viven en main.py, no en módulos aparte.
get_current_user = main.get_current_user
get_db = main.get_db

client = TestClient(main.app, raise_server_exceptions=True)
main.app.dependency_overrides[get_db] = lambda: db

HTTP_UNI = "UNLZ"

# --- enganche 1: el alta de Intervalo clásico ---
# Nueve ya adentro; la décima entra por el endpoint.
for _ in range(9):
    alta_clasico(HTTP_UNI, datetime.utcnow())
db.commit()
check(boosts.multiplier_for(db, HTTP_UNI) == 1.0, "con nueve todavía no hay empuje")

_seq += 1
decima = User(email="http10@x.com", name="Decima", username="http10",
              created_at=datetime.utcnow())
db.add(decima)
db.commit()
main.app.dependency_overrides[get_current_user] = lambda: decima
r = client.post("/user/enroll", json={"course": "analisis", "university": HTTP_UNI, "career": "ING"})
check(r.status_code == 200, f"POST /user/enroll devuelve 200 (dio {r.status_code})")
check(abs(boosts.multiplier_for(db, HTTP_UNI) - 1.5) < 1e-9,
      f"y el alta número 10 prendió el empuje sola (dio {boosts.multiplier_for(db, HTTP_UNI)})")

# Un re-enrollment de la misma persona no puede prender nada nuevo mañana.
antes = db.query(GameBoost).filter(GameBoost.source == aforo.SOURCE).count()
client.post("/user/enroll", json={"course": "analisis", "university": HTTP_UNI, "career": "ING"})
check(db.query(GameBoost).filter(GameBoost.source == aforo.SOURCE).count() == antes,
      "y un re-enrollment no cuenta como alguien nuevo")
main.app.dependency_overrides.pop(get_current_user, None)

# --- enganche 2: cargar la universidad en el minijuego ---
JUEGO_UNI = "UNQ"
for _ in range(9):
    jugador(JUEGO_UNI, datetime.utcnow())
db.commit()
check(boosts.multiplier_for(db, JUEGO_UNI) == 1.0, "con nueve jugadores tampoco")

r = client.post("/game/derivemos/player", json={})
check(r.status_code == 200, f"POST /player devuelve 200 (dio {r.status_code})")
token = r.json()["guest_token"]
r = client.patch("/game/derivemos/me", headers={"X-Game-Token": token},
                 json={"university": JUEGO_UNI})
check(r.status_code == 200, f"PATCH /me devuelve 200 (dio {r.status_code})")
check(abs(boosts.multiplier_for(db, JUEGO_UNI) - 1.5) < 1e-9,
      f"y el décimo jugador que carga su universidad lo prende (dio {boosts.multiplier_for(db, JUEGO_UNI)})")

# El PATCH que no toca la universidad no puede prender nada.
r = client.patch("/game/derivemos/me", headers={"X-Game-Token": token}, json={"career": "ING"})
check(r.status_code == 200, "un PATCH de carrera sola sigue andando (la variable existe)")

main.app.dependency_overrides.clear()


print()
if FAILURES:
    print(f"{len(FAILURES)} chequeos fallaron:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("todos los chequeos pasaron")
