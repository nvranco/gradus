"""Verifica los mails del cruce: cafecito, reclutas y el "volvé" del minijuego.

Lo que importa de todos no es que se manden, sino CUÁNDO NO se mandan:

· Un mail que dice «tu cafecito generó 0 XP» es peor que ningún mail.
· Un resumen semanal vacío convierte el canal en ruido y enseña a ignorarlo, que
  es lo último que se quiere de algo que llega una vez por semana.

Y una promesa que el mail del cafecito NO puede hacer: `multiplier_for` colapsa
el empuje global y el dirigido en un número, y los cafecitos de la ventana se
suman a propósito, así que la XP no se puede atribuir a UNA donación. El copy
dice «el empuje de tu universidad», nunca «tu cafecito».

Y el caso que estaba mal del otro lado: un usuario que existe SOLO por el juego
cumplía para siempre la condición del bounce —«ninguna sesión de Intervalo
terminada»— y recibía «Solo falta tu primera sesión» habiendo jugado cien
derivadas, mientras que el winback, que pide una sesión terminada, no le llegaba
nunca.

Uso:
    python backend/scripts/check_mails_cafecito_reclutas.py

No manda nada: el envío se intercepta.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = "sqlite:///" + str(
    Path(tempfile.mkdtemp()) / "mails.db"
).replace("\\", "/")
os.environ.setdefault("EMAIL_UNSUB_SECRET", "test")
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent))

import database  # noqa: E402
from models import Base, User  # noqa: E402

Base.metadata.create_all(database.engine)

import lifecycle_emails as le  # noqa: E402

fallos: list[str] = []
enviados: list[dict] = []


def check(nombre: str, cond: bool, detalle: str = "") -> None:
    print(f"{'ok   ' if cond else 'FALLA'}  {nombre} {detalle}".rstrip())
    if not cond:
        fallos.append(nombre)


# Se intercepta el envío: este check no habla con Resend.
def _fake_send(to_email, subject, html, unsubscribe_url, text=None):
    enviados.append({"to": to_email, "subject": subject, "html": html})
    return True


le._send = _fake_send

db = database.SessionLocal()
db.add(User(id=1, clerk_user_id="c1", email="a@a.com", name="Ana Gómez"))
db.commit()
u = db.get(User, 1)

print("1. el mail del cafecito no se manda si no hubo efecto")
check("con 0 XP extra no se manda",
      not le.send_cafecito_efecto_email(db, u, university="UBA", xp_extra=0, estudiantes=0))
check("y no salió nada", len(enviados) == 0)

print("2. con efecto real sí, y dice lo que puede afirmar")
ok = le.send_cafecito_efecto_email(db, u, university="UBA", xp_extra=340, estudiantes=12)
check("se manda", ok and len(enviados) == 1)
mail = enviados[-1]
check("el asunto lleva el número y la universidad",
      "340" in mail["subject"] and "UBA" in mail["subject"], f"({mail['subject']!r})")
check("el cuerpo habla del empuje de la universidad, NO de 'tu cafecito generó'",
      "la UBA sumó 340 XP extra" in mail["html"])
check("y dice entre cuántos se repartió", "12 estudiantes" in mail["html"])

print("3. el empuje global se nombra distinto")
le.send_cafecito_efecto_email(db, u, university=None, xp_extra=90, estudiantes=5)
mail = enviados[-1]
check("sin universidad dice 'todo Intervalo'", "todo Intervalo" in mail["subject"])

print("4. el resumen de reclutas no se manda vacío")
antes = len(enviados)
check("sin XP en la semana no se manda",
      not le.send_reclutas_semanal_email(db, u, xp_semana=0, filas=[]))
check("con XP pero sin filas tampoco",
      not le.send_reclutas_semanal_email(db, u, xp_semana=10, filas=[]))
check("y no salió nada", len(enviados) == antes)

print("5. con movimiento, el listado va ordenado y completo")
filas = [("lucia.m", "UBA", 120), ("tomifer", "UTN", 64), ("nachoq", "UBA", 31)]
ok = le.send_reclutas_semanal_email(db, u, xp_semana=215, filas=filas)
check("se manda", ok)
mail = enviados[-1]
check("el asunto lleva el total", "215 XP" in mail["subject"], f"({mail['subject']!r})")
check("el destacado dice de cuántas personas", "de 3 personas" in mail["html"])
for alias, uni, xp in filas:
    check(f"la fila de @{alias} está", f"@{alias}" in mail["html"] and f"{xp} XP" in mail["html"])
check("va como tabla y no como flex, que los clientes de correo no soportan",
      "<table" in mail["html"])
check("y el botón dice Ver", ">Ver<" in mail["html"])

print("6. el PRODUCTOR: quién recibe el resumen, y con qué números")
# Las secciones de arriba prueban el renderizado con datos escritos a mano. Esto
# prueba quién los arma, que es donde estaba el bug: el mail decía «esta semana»
# con el acumulado histórico, y la guarda de «sin movimiento no se manda» nunca
# se activaba porque `referral_xp_given > 0` es verdadero para siempre.
from models import Course, Enrollment, GamePlayer  # noqa: E402

db.add(Course(id=1, slug="analisis", name="Análisis"))
db.commit()
db.add(GamePlayer(id=7, alias="ana", user_id=1))
db.commit()
db.add(User(id=2, clerk_user_id="c2", email="b@b.com", name="Tomi",
            username="tomi", referred_by_player_id=7, referral_xp_given=40))
db.add(User(id=3, clerk_user_id="c3", email="c@c.com", name="Lu",
            username="lu", referred_by_player_id=7, referral_xp_given=15))
db.commit()
db.add(Enrollment(user_id=2, course_id=1, university="UTN"))
db.add(Enrollment(user_id=3, course_id=1, university="UBA"))
db.commit()

pendientes = le.due_reclutas_semanal_emails(db)
check("con XP nueva, la persona entra", len(pendientes) == 1, f"(dio {len(pendientes)})")
r = pendientes[0]
check("el total es la suma de lo nuevo", r.datos["xp_semana"] == 55,
      f"(dio {r.datos['xp_semana']})")
check("las filas van de mayor a menor", [f[2] for f in r.datos["filas"]] == [40, 15],
      f"(dio {[f[2] for f in r.datos['filas']]})")
# La universidad venía SIEMPRE vacía: el productor la armaba como `""` y el mail
# dibujaba una columna en blanco.
check("y cada fila lleva su universidad",
      [f[1] for f in r.datos["filas"]] == ["UTN", "UBA"],
      f"(dio {[f[1] for f in r.datos['filas']]})")

print("7. la marca corta la semana: sin XP nueva no se manda otra vez")
antes = len(enviados)
resumen = le.run_lifecycle_emails(db)
check("la corrida manda uno", resumen["reclutas_sent"] == 1,
      f"(dio {resumen['reclutas_sent']})")
check("y salió de verdad", len(enviados) == antes + 1)
check("las marcas quedaron paradas en lo ya contado",
      db.get(User, 2).referral_xp_email_seen == 40
      and db.get(User, 3).referral_xp_email_seen == 15)

# Una semana después, sin XP nueva. Es la aserción que faltaba: la ventana de
# `reclutas_email_sent_on` tapaba el bug, así que hay que preguntar con la fecha
# ya corrida.
from datetime import datetime, timedelta  # noqa: E402

db.get(User, 1).reclutas_email_sent_on = (
    datetime.utcnow().date() - timedelta(days=8)
)
db.commit()
check("sin movimiento, la semana siguiente no manda nada",
      le.due_reclutas_semanal_emails(db) == [])

db.get(User, 2).referral_xp_given = 65
db.commit()
otra = le.due_reclutas_semanal_emails(db)
check("pero con 25 XP más sí vuelve", len(otra) == 1)
if otra:
    check("y cuenta 25, no 80", otra[0].datos["xp_semana"] == 25,
          f"(dio {otra[0].datos['xp_semana']})")
    check("con una sola fila, la que se movió",
          len(otra[0].datos["filas"]) == 1)

# ── El "volvé" del minijuego, y el bounce que le mentía ──────────────────────
#
# Estas dos van juntas porque son el mismo hecho visto de los dos lados: un
# usuario que existe SOLO por el juego. Hasta acá, ese usuario cumplía para
# siempre la condición del bounce —«ninguna sesión de Intervalo terminada»— y
# recibía «Solo falta tu primera sesión» habiendo jugado cien derivadas, y en
# cambio no podía recibir nunca el winback, que pide una sesión terminada.
from datetime import datetime, timedelta  # noqa: E402

print()
print("— el volvé del juego —")
enviados.clear()

u_dx = User(id=50, clerk_user_id="dx50", email="dx@x.com", name="Dedé",
            created_at=datetime.utcnow() - timedelta(days=30))
db.add(u_dx)
db.flush()
p_dx = GamePlayer(
    id=50, alias="dede", user_id=50, exercises_correct=40,
    last_seen_at=datetime.utcnow() - timedelta(days=6),
)
db.add(p_dx)
db.commit()

check("el bounce ya no le sale a quien jugó",
      all(u.id != 50 for u in le.due_bounce_emails(db)))
# Y le sigue saliendo a quien de verdad no hizo nada.
u_vacio = User(id=51, clerk_user_id="dx51", email="vacio@x.com", name="Vacío",
               created_at=datetime.utcnow() - timedelta(days=30))
db.add(u_vacio)
db.commit()
check("pero le sigue saliendo a quien no hizo nada",
      any(u.id == 51 for u in le.due_bounce_emails(db)))

pendientes = le.due_winback_dx_emails(db)
check("a los 6 días sin derivar toca el volvé", [u.id for u, _ in pendientes] == [50],
      f"({[u.id for u, _ in pendientes]})")
check("y se manda", le.send_winback_dx_email(db, u_dx, p_dx))
# El asunto es EL MISMO que el de Intervalo, a propósito: mismo remitente y una
# sola marca, así que dos asuntos distintos para el mismo mensaje se leen como
# dos productos peleándose la atención de la misma persona. Lo que lleva a cada
# lado es el botón.
check("el asunto es el mismo que el de Intervalo",
      enviados[-1]["subject"] == "¡Volvé Dedé! 👀", enviados[-1]["subject"])
check("y el cuerpo es el mismo salvo la palabra que cambia",
      "Tus derivadas te extrañan y te están sacando puestos en el ranking."
      in enviados[-1]["html"] and "Recuperalos hoy mismo." in enviados[-1]["html"])
check("y el botón lleva al juego y no a la home",
      "/derivadas?utm_source=email" in enviados[-1]["html"])
check("no vuelve mientras no juegue", le.due_winback_dx_emails(db) == [])

p_dx.last_seen_at = datetime.utcnow()
db.commit()
check("y el que volvió a jugar tampoco recibe",
      [u.id for u, _ in le.due_winback_dx_emails(db)] == [])
# La secuencia real: se le mandó un mail hace diez días, volvió a jugar después
# de eso, y hace seis que no aparece. Ahí SÍ se re-arma — un mail por ausencia y
# no uno por semana. Se mueve la marca y no `last_seen_at`, porque el reloj no
# va para atrás: alguien "vuelve" jugando, no dejando de jugar más temprano.
p_dx.winback_email_sent_at = datetime.utcnow() - timedelta(days=10)
p_dx.last_seen_at = datetime.utcnow() - timedelta(days=6)
db.commit()
check("pero si volvió y se fue otra vez, se re-arma",
      [u.id for u, _ in le.due_winback_dx_emails(db)] == [50],
      f"({[u.id for u, _ in le.due_winback_dx_emails(db)]})")

# El invitado no puede entrar y no es un olvido: no tiene mail.
db.add(GamePlayer(id=52, alias="invitado", user_id=None, exercises_correct=10,
                  last_seen_at=datetime.utcnow() - timedelta(days=10)))
db.commit()
check("al invitado no se le manda nada, porque no tiene mail",
      all(p.id != 52 for _, p in le.due_winback_dx_emails(db)))

# Y el que se dio de baja no recibe, como todos los demás.
u_dx.email_unsubscribed = True
db.commit()
check("y el que se dio de baja tampoco", le.due_winback_dx_emails(db) == [])
u_dx.email_unsubscribed = False
db.commit()

db.close()

print()
if fallos:
    print(f"{len(fallos)} chequeos fallaron:")
    for f in fallos:
        print(f"  - {f}")
    raise SystemExit(1)
print("todo ok")
