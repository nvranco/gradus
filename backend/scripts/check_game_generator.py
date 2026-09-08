"""Verifica el generador, el Elo y el validador del minijuego de derivadas.

Cubre la Fase 1 del plan:
  1. Cada plantilla genera 20 ejercicios y sympy.diff coincide con la derivada
     esperada re-parseada del string persistido (round-trip str->sympify).
  2. El validador acepta la derivada correcta y formas algebraicamente
     equivalentes; rechaza incorrectas; los errores predecibles devuelven su
     feedback específico; expresiones patológicas se rechazan sin decidir.
  3. La rampa inicial sirve tiers crecientes y la banda objetivo selecciona
     plantillas razonables; el update de Elo mueve θ/β en la dirección correcta.
  4. El piso de Elo de las trigonométricas: debajo de la barrera no se cuela
     ninguna por NINGÚN camino de pick_template (rampa, tope del salteo y los
     fallbacks que se quedan sin candidatos), y encima vuelven a estar en juego.

Uso:
    python backend/scripts/check_game_generator.py

Determinístico (seeds fijas). Sale con código 1 si algo falla.
"""

import os
import random
import sys
import tempfile
from pathlib import Path

# La consola de Windows viene en cp1252; los labels usan ≡/·/→.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = "sqlite:///" + str(
    Path(tempfile.mkdtemp()) / "game.db"
).replace("\\", "/")
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent))

import sympy  # noqa: E402
from sympy import Symbol, cos, exp, log, sin  # noqa: E402

import database  # noqa: E402
from models import Base, GamePlayer  # noqa: E402
from game import elo  # noqa: E402
from game import keyboard as game_keyboard  # noqa: E402
from game.cycler import CyclingRandom  # noqa: E402
from game.generator import _build_cycled, pick_template, serve_exercise  # noqa: E402
from game.mathjson import MathJsonError, to_sympy  # noqa: E402
from game.templates import TEMPLATES, latex_es  # noqa: E402


def build(template, rng, state=None):
    """`template.build` ahora pide un CyclingRandom. Estado fresco por default:
    la mayoría de estos chequeos no le importa el ciclado, solo que la
    plantilla siga generando instancias válidas."""
    return template.build(CyclingRandom(rng, {} if state is None else state))
from game.validator import (  # noqa: E402
    AnswerRejected,
    expr_from_stored,
    guard_candidate,
    match_common_error,
    numerically_equivalent,
)

x = Symbol("x")
FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        FAILURES.append(label)


def check_raises(fn, exc_type, label: str) -> None:
    try:
        fn()
    except exc_type:
        check(True, label)
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {label} (levantó {type(exc).__name__}: {exc})")
        FAILURES.append(label)
    else:
        check(False, label)


# ── 1. Generación: 20 por plantilla, derivada correcta y round-trip ──────────
print("1. plantillas (20 ejercicios c/u)")
rng = random.Random(20260827)
for template in TEMPLATES:
    ok_diff = ok_roundtrip = ok_latex = ok_errors = True
    for _ in range(20):
        generated = build(template, rng)
        derivative = sympy.diff(generated.f, x)
        reparsed = expr_from_stored(str(derivative))
        if sympy.simplify(reparsed - derivative) != 0:
            ok_roundtrip = False
        # La derivada esperada nunca puede ser idéntica a un error predecible
        # (si no, el "error" sería correcto).
        for wrong, _fb in generated.common_errors:
            if sympy.simplify(wrong - derivative) == 0:
                ok_errors = False
        prompt = generated.prompt_latex or latex_es(generated.f)
        # `\tan` ya no se traduce: es la notación que muestra el teclado y la
        # tabla. El que sigue prohibido es `\sin`, que en el juego es `sen`.
        if not prompt or "\\sin" in prompt:
            ok_latex = False
        if derivative.has(sympy.Derivative):
            ok_diff = False
    check(ok_diff, f"{template.key}: deriva sin residuos")
    check(ok_roundtrip, f"{template.key}: round-trip str->sympify")
    check(ok_latex, f"{template.key}: latex en notación es (sen)")
    check(ok_errors, f"{template.key}: ningún error predecible == derivada")

# ── 2. Validador ─────────────────────────────────────────────────────────────
print("2. validador")
# f = x²·sen x → f' = 2x·sen x + x²·cos x
expected = sympy.diff(x**2 * sin(x), x)
check(numerically_equivalent(expected, 2 * x * sin(x) + x**2 * cos(x)), "acepta la forma directa")
check(numerically_equivalent(expected, x * (2 * sin(x) + x * cos(x))), "acepta forma factorizada")
check(not numerically_equivalent(expected, 2 * x * cos(x)), "rechaza u'·v'")

# Equivalencia trigonométrica no trivial: 2·sen x·cos x ≡ sen(2x)
check(numerically_equivalent(2 * sin(x) * cos(x), sin(2 * x)), "2·sen·cos ≡ sen(2x)")

# ln: grilla positiva
expected_ln = sympy.diff(log(x), x)
check(numerically_equivalent(expected_ln, 1 / x), "ln x: acepta 1/x")
check(not numerically_equivalent(expected_ln, 1 / x**2), "ln x: rechaza 1/x²")

# a^x con ln a en forma numérica equivalente
expected_ax = sympy.diff(sympy.Integer(3) ** x, x)
check(
    numerically_equivalent(expected_ax, sympy.Integer(3) ** x * log(3)),
    "3^x: acepta 3^x·ln 3",
)
check(not numerically_equivalent(expected_ax, sympy.Integer(3) ** x), "3^x: rechaza sin ln 3")

# Guardas
check_raises(lambda: guard_candidate(Symbol("y") + x), AnswerRejected, "rechaza símbolo extra")
check_raises(lambda: guard_candidate(x ** sympy.Integer(99)), AnswerRejected, "rechaza exponente enorme")
check_raises(
    lambda: guard_candidate(sympy.Integer(10) ** 20 * x), AnswerRejected, "rechaza enteros gigantes"
)
check_raises(
    lambda: numerically_equivalent(expected_ln, sympy.sqrt(-1 - x**2)),
    AnswerRejected,
    "sin puntos co-válidos no decide",
)

# Feedback específico por error predecible
tpl = next(t for t in TEMPLATES if t.key == "t4_pow_sin")
gen = build(tpl, random.Random(7))
import json  # noqa: E402

errors_json = json.dumps(
    [{"expr": str(e), "feedback": fb} for e, fb in gen.common_errors]
)
wrong_expr, wrong_fb = gen.common_errors[0]
check(match_common_error(errors_json, wrong_expr) == wrong_fb, "matchea error predecible (producto)")
check(match_common_error(errors_json, sympy.diff(gen.f, x)) is None, "la correcta no matchea errores")

# ── 3. MathJSON ──────────────────────────────────────────────────────────────
print("3. mathjson")
mj = ["Add", ["Multiply", 2, "x", ["Sin", "x"]], ["Multiply", ["Power", "x", 2], ["Cos", "x"]]]
check(sympy.simplify(to_sympy(mj) - expected) == 0, "árbol de producto completo")
check(to_sympy(["Divide", 1, "x"]) == 1 / x, "División")
check(to_sympy(["Multiply", ["Power", "ExponentialE", "x"], ["Ln", "x"]]) == exp(x) * log(x), "e^x·ln x")
check(to_sympy(["Log", "x", 3]) == log(x, 3), "log base 3")
check_raises(lambda: to_sympy(["Integrate", "x"]), MathJsonError, "head desconocido")
check_raises(lambda: to_sympy("z"), MathJsonError, "símbolo desconocido")

# ── 4. Rampa + banda + Elo con BD ────────────────────────────────────────────
print("4. selección y Elo")
Base.metadata.create_all(bind=database.engine)
db = database.SessionLocal()
player = GamePlayer(guest_token="check-token", alias="checker1")
db.add(player)
db.commit()
db.refresh(player)

rng = random.Random(1)
tiers_seen = []
for n in range(5):
    player.n_updates = n
    template, stat, p_hat = pick_template(db, player, rng)
    tiers_seen.append(template.tier)
check(tiers_seen == sorted(tiers_seen) and tiers_seen[0] == 0, f"rampa por tier creciente {tiers_seen}")

player.n_updates = 50
player.theta = 0.0
picks = {pick_template(db, player, random.Random(i))[0].tier for i in range(30)}
check(all(t <= 4 for t in picks), f"con θ=0 la banda elige tiers bajos/medios {sorted(picks)}")

theta1, beta1 = elo.update(0.0, 0, 0.0, 0, correct=True)
theta2, beta2 = elo.update(0.0, 0, 0.0, 0, correct=False)
check(theta1 > 0 and beta1 < 0, "acierto: sube θ, baja β")
check(theta2 < 0 and beta2 > 0, "fallo: baja θ, sube β")
check(abs(elo.update(0.0, 100, 0.0, 0, True)[0]) < abs(theta1), "learning rate decrece con n")

# ── Ancla de β a la semilla del tier ─────────────────────────────────────────
# Lo que se defiende acá es que una plantilla que vio poca GENTE no pueda dar
# vuelta la escalera de dificultad. El caso real: en el primer día de producción
# `t5_pow_over_linear` tenía 12 observaciones de 2 personas y `t3_ax` 11 de 2,
# porque el motor solo le sirve lo difícil a los que van bien. Contadas como 12 y
# 11 respuestas parecían plantillas conocidas; contadas como 2 personas, no.
print("\n— ancla de β —")
K = elo.BETA_PRIOR_PLAYERS
check(elo.effective_beta(-9.0, 5, 0) == elo.BETA_SEED[5],
      "sin nadie que la haya visto, la β creída ES la semilla")
mitad = elo.effective_beta(-9.0, 5, int(K))
check(abs(mitad - (-9.0 + elo.BETA_SEED[5]) / 2) < 1e-9,
      f"con K personas queda a mitad de camino ({mitad:.2f})")
check(abs(elo.effective_beta(-9.0, 5, 2000) - (-9.0)) < 0.06,
      "con mucha gente la semilla se lava sola")
check(elo.BETA_SEED[5] > elo.effective_beta(-9.0, 5, 3) > -9.0,
      "y siempre queda entre la semilla y lo aprendido")

# EL punto de contar personas y no respuestas: dos plantillas con la MISMA β
# aprendida y las mismas observaciones, pero una vista por mucha gente y la otra
# por dos personas, no pueden valer lo mismo.
mucha_gente = elo.effective_beta(-0.36, 5, 20)
dos_personas = elo.effective_beta(-0.36, 5, 2)
check(dos_personas > mucha_gente,
      f"lo que vieron 2 personas queda más cerca de la semilla que lo que vieron 20 "
      f"({dos_personas:.2f} vs {mucha_gente:.2f})")
check(abs(dos_personas - elo.BETA_SEED[5]) < abs(dos_personas - (-0.36)),
      "con 2 personas manda la semilla, no lo aprendido")

# Regresión con la foto de producción del 28/08: 26 plantillas, un tercio de las
# respuestas de un solo estudiante. Una INVERSIÓN es un par de plantillas de
# tiers distintos donde la del tier más bajo quedó más difícil que la del más
# alto: la escalera dada vuelta.
#
# Los números son una FOTO, no una verdad eterna: si el banco cambia se releen
# de la base. Lo que se defiende no es el 9 sino que el ancla reduzca mucho las
# inversiones y devuelva el orden entre tiers.
# (clave, tier, β cruda, observaciones, personas distintas)
BETAS_28_08 = [
    ("t0_const", 0, -3.08, 16, 11), ("t0_x", 0, -2.45, 54, 45),
    ("t1_kpow", 1, -2.39, 23, 16), ("t1_kx", 1, -3.28, 11, 6), ("t1_pow", 1, -3.10, 34, 24),
    ("t2_pow_plus_const", 2, -2.93, 13, 7), ("t2_sum2", 2, -2.85, 20, 10),
    ("t2_sum3", 2, -2.48, 48, 31),
    ("t3_ax", 3, -0.42, 11, 2), ("t3_cos", 3, -2.76, 17, 8), ("t3_exp", 3, -2.31, 12, 7),
    ("t3_ln", 3, -1.72, 13, 8), ("t3_loga", 3, -0.21, 16, 6), ("t3_mix_sum", 3, -1.44, 23, 14),
    ("t3_sin", 3, -2.70, 16, 7), ("t3_trig_sum", 3, -1.91, 19, 13),
    ("t4_exp_cos", 4, -0.77, 17, 4), ("t4_exp_sin", 4, -1.52, 14, 8),
    ("t4_pow_exp", 4, -1.78, 14, 9), ("t4_pow_ln", 4, -2.05, 17, 10),
    ("t4_pow_sin", 4, -0.92, 13, 8),
    ("t5_exp_over_pow", 5, 0.02, 13, 4), ("t5_linear_over_linear", 5, -0.77, 19, 5),
    ("t5_ln_over_x", 5, 1.69, 1, 1), ("t5_pow_over_linear", 5, -0.36, 12, 2),
    ("t5_sin_over_x", 5, 1.67, 1, 1),
]


def _inversiones(pares: list[tuple[int, float]]) -> int:
    return sum(1 for i, (ta, ba) in enumerate(pares)
               for tb, bb in pares[i + 1:] if ta < tb and ba > bb)


def _medias(pares: list[tuple[int, float]]) -> list[float]:
    m: dict[int, list[float]] = {}
    for t, b in pares:
        m.setdefault(t, []).append(b)
    return [sum(m[t]) / len(m[t]) for t in sorted(m)]


crudas = [(t, b) for _, t, b, _, _ in BETAS_28_08]
ancladas = [(t, elo.effective_beta(b, t, gente)) for _, t, b, _, gente in BETAS_28_08]
inv_cruda, inv_anclada = _inversiones(crudas), _inversiones(ancladas)
check(inv_cruda >= 30, f"la foto de producción tenía la escalera dada vuelta ({inv_cruda} inversiones)")
check(inv_anclada <= inv_cruda / 3, f"y el ancla la endereza ({inv_cruda} → {inv_anclada})")
check(not _medias(crudas) == sorted(_medias(crudas)), "las medias por tier crudas no subían")
orden = _medias(ancladas)
check(orden == sorted(orden),
      "y ancladas vuelven a subir monótonas: "
      + " < ".join(f"T{t} {m:+.2f}" for t, m in zip(sorted({t for t, _ in ancladas}), orden)))

# Contar personas gana justo donde tiene que ganar: las plantillas que vieron
# 2 personas quedan más cerca de su semilla que contándolas por respuestas.
POR_RESPUESTAS = {k: (n, gente) for k, _, _, n, gente in BETAS_28_08}
for clave in ("t5_pow_over_linear", "t3_ax", "t4_exp_cos"):
    _, tier, cruda, n_obs, gente = next(r for r in BETAS_28_08 if r[0] == clave)
    semilla = elo.BETA_SEED[tier]
    por_gente = elo.effective_beta(cruda, tier, gente)
    por_obs = elo.effective_beta(cruda, tier, n_obs)
    check(abs(por_gente - semilla) <= abs(por_obs - semilla),
          f"{clave} ({n_obs} respuestas de {gente} personas): contar gente la deja "
          f"en {por_gente:+.2f} y contar respuestas en {por_obs:+.2f} (semilla {semilla:+.2f})")

# La otra mitad del arreglo: con el ancla, la sorpresa que la plantilla ya no se
# come se la lleva la persona. θ tiene que moverse MÁS, no igual.
t_anclado = elo.update(0.1, 5, -2.05, 17, correct=True, tier=4, n_players=10)[0]
t_crudo = elo.update(0.1, 5, -2.05, 17, correct=True)[0]
check(t_anclado - 0.1 > 1.5 * (t_crudo - 0.1),
      f"θ se mueve más rápido con el ancla (+{t_anclado - 0.1:.3f} vs +{t_crudo - 0.1:.3f})")
check(elo.update(0.1, 5, -2.05, 17, True, tier=4, n_players=10)[1]
      == elo.update(0.1, 5, -2.05, 17, True)[1],
      "pero la β guardada se sigue corrigiendo contra su propia p̂ cruda")

elo_k = elo.BETA_PRIOR_PLAYERS
elo.BETA_PRIOR_PLAYERS = 0.0
check(elo.effective_beta(-9.0, 5, 17) == -9.0, "con BETA_PRIOR_PLAYERS=0 el ancla se apaga entera")
elo.BETA_PRIOR_PLAYERS = elo_k
print()

served = serve_exercise(db, player)
db.commit()
check(served.status == "served" and served.expected_derivative, "serve_exercise persiste")
served2 = serve_exercise(db, player)
db.commit()
db.refresh(served)
check(served.status == "expired", "servir de nuevo expira el anterior")
check(served2.template_key != served.template_key, "anti-repetición inmediata")
db.close()

# ── Ciclado de números ───────────────────────────────────────────────────────
# t1_pow tiene una sola ranura ("n", dominio {2,3,4,5}): agotarla una vuelta
# tiene que mostrar los 4 valores sin repetir ninguno, y la vuelta siguiente
# vuelve a barajar (no repite el mismo orden).
print("\n— ciclado de números —")
tpl_pow = next(t for t in TEMPLATES if t.key == "t1_pow")
estado: dict[str, list] = {}
vuelta1 = [build(tpl_pow, random.Random(i), estado).f for i in range(4)]
check(sorted(str(sympy.diff(f, x)) for f in vuelta1) == sorted(str(sympy.diff(x**n, x)) for n in (2, 3, 4, 5)),
      f"una vuelta completa sirve los 4 exponentes sin repetir ({[str(f) for f in vuelta1]})")
vuelta2 = [build(tpl_pow, random.Random(100 + i), estado).f for i in range(4)]
check(sorted(str(f) for f in vuelta2) == sorted(str(f) for f in vuelta1),
      "la segunda vuelta vuelve a servir los mismos 4 exponentes")
# Namespacing en _build_cycled: t0_const y t1_kx tienen las dos una ranura "k"
# — persistidas en el mismo blob (numeric_cycle_json) no se pueden pisar.
class _JugadorFalso:
    numeric_cycle_json = "{}"

falso = _JugadorFalso()
tpl_const, tpl_kx = (next(t for t in TEMPLATES if t.key == k) for k in ("t0_const", "t1_kx"))
_build_cycled(falso, tpl_const, random.Random(1))
blob_tras_const = json.loads(falso.numeric_cycle_json)
_build_cycled(falso, tpl_kx, random.Random(1))
blob_tras_kx = json.loads(falso.numeric_cycle_json)
check(
    "t0_const:k" in blob_tras_const and "t0_const:k" in blob_tras_kx,
    "servir t1_kx no pisa la ranura 'k' que ya tenía t0_const",
)
check("t1_kx:k" in blob_tras_kx, "y t1_kx guarda la suya bajo su propio prefijo")

# ── 5. Teclado acumulativo ───────────────────────────────────────────────────
# El riesgo real es dejar a alguien sin poder escribir la respuesta: se verifica
# que TODA tecla que la derivada exige esté desbloqueada después de servirla.
print("5. teclado acumulativo")
rng = random.Random(20260827)
samples: dict[str, list[str]] = {}
for template in TEMPLATES:
    ok_covers = ok_vocab = True
    for i in range(20):
        generated = build(template, rng)
        derivative = sympy.diff(generated.f, x)
        required = game_keyboard.required_keys(derivative)
        col, fresh = game_keyboard.unlock("", derivative)
        keys = game_keyboard.parse_unlocked_ordered(col)
        if not required.issubset(keys):
            ok_covers = False
        if not set(keys).issubset(game_keyboard.CANONICAL_ORDER):
            ok_vocab = False
        # Desde cero, todo lo exigido es nuevo: no hay teclas de relleno.
        if set(fresh) != required:
            ok_covers = False
        samples.setdefault(template.key, keys)
    check(ok_covers, f"{template.key}: desbloquea exactamente lo necesario")
    check(ok_vocab, f"{template.key}: vocabulario conocido")

# Lo que define al inventario: nunca encoge, y recorrer todas las plantillas
# termina desbloqueando el vocabulario entero.
col = ""
sizes: list[int] = []
for template in TEMPLATES:
    for _ in range(5):
        derivative = sympy.diff(build(template, rng).f, x)
        col, _fresh = game_keyboard.unlock(col, derivative)
        sizes.append(len(game_keyboard.parse_unlocked(col)))
check(all(b >= a for a, b in zip(sizes, sizes[1:])), "el inventario nunca encoge")

# Alcanzable = lo que alguna plantilla llega a exigir. Se calcula de las
# plantillas y no se hardcodea: si mañana entra una con raíces o tangente, este
# chequeo se entera solo.
alcanzable: set[str] = set()
rng_cov = random.Random(20260828)
for template in TEMPLATES:
    for _ in range(60):
        alcanzable |= game_keyboard.required_keys(sympy.diff(build(template, rng_cov).f, x))
final = game_keyboard.parse_unlocked(col)
check(final == alcanzable, f"se desbloquea todo lo alcanzable ({len(final)}/{len(alcanzable)})")

# Las que ningún ejercicio puede pedir. NO es un fallo: es el dato de que esas
# teclas del vocabulario están muertas mientras no exista una plantilla que las
# necesite — antes se veían igual porque entraban como distractores, y con el
# teclado acumulativo ya no aparecen nunca.
inalcanzables = [k for k in game_keyboard.CANONICAL_ORDER if k not in alcanzable]
print(f"   alcanzables: {' '.join(game_keyboard.in_order(alcanzable))}")
print(f"   sin plantilla que las pida: {' '.join(inalcanzables) or '(ninguna)'}")
# Volver a servir algo ya visto no vuelve a anunciarlo como nuevo.
repetida = sympy.diff(build(TEMPLATES[0], rng).f, x)
_col, fresh_otra_vez = game_keyboard.unlock(col, repetida)
check(fresh_otra_vez == [], "una tecla ya desbloqueada no se reanuncia")

print("   muestras (desde cero, una plantilla sola):")
for key, keys in samples.items():
    print(f"     {key:22s} {' '.join(keys) if keys else '(sin teclas nuevas)'}")

# ── 6. Piso de Elo de las trigonometricas ────────────────────────────────────
print()
print("6. piso de Elo")
from game.generator import desbloqueadas  # noqa: E402
from game.templates import PISO_TRIGONOMETRICAS  # noqa: E402

CON_PISO = {t.key for t in TEMPLATES if t.min_rating is not None}
check(CON_PISO == {"t3_sin", "t3_cos", "t3_tan", "t3_trig_sum",
                   "t4_pow_sin", "t4_exp_cos", "t4_exp_sin", "t5_sin_over_x"},
      f"el piso cubre las 8 plantillas con seno, coseno o tangente (dio {sorted(CON_PISO)})")

# El θ justo debajo y justo encima de la barrera. rating_of redondea, así que se
# toma un paso de un punto entero de rating para no depender del redondeo.
theta_piso = (PISO_TRIGONOMETRICAS - elo.RATING_BASE) / elo.RATING_PER_THETA
ABAJO = theta_piso - 1 / elo.RATING_PER_THETA
JUSTO = theta_piso

gate = GamePlayer(guest_token="check-piso", alias="checkpiso")
db.add(gate)
db.commit()
db.refresh(gate)

gate.theta = ABAJO
check(elo.rating_of(gate.theta) < PISO_TRIGONOMETRICAS, "el jugador de prueba está debajo de la barrera")
check(not (CON_PISO & {t.key for t in desbloqueadas(gate)}),
      "debajo del piso no hay ninguna trigonométrica desbloqueada")
gate.theta = JUSTO
check(CON_PISO <= {t.key for t in desbloqueadas(gate)},
      "al tocar la barrera se desbloquean las ocho de una")

# Lo que importa no es la función pura sino que NINGÚN camino de pick_template
# la esquive: ni la rampa, ni el tope del salteo, ni los fallbacks que se
# quedan sin candidatos. Se barre θ de −2 a la barrera, con y sin tope.
gate.theta = ABAJO
servidas = set()
for n_updates in (0, 1, 2, 3, 4, 50):
    gate.n_updates = n_updates
    for theta in (-2.0, -1.0, -0.5, 0.0, 0.4, 0.8, ABAJO):
        gate.theta = theta
        for seed in range(25):
            servidas.add(pick_template(db, gate, random.Random(seed))[0].key)
            for tope in (-1, 0, 1, 2, 3, 4, 5):
                servidas.add(
                    pick_template(db, gate, random.Random(seed), max_tier=tope)[0].key
                )
colados = sorted(CON_PISO & servidas)
check(not colados, f"debajo del piso no se cuela ninguna por ningún camino (se colaron: {colados})")
check(len(servidas) >= 10, f"y queda banco de sobra para elegir ({len(servidas)} plantillas distintas)")

# Y del otro lado de la barrera vuelven a estar en juego, que es la mitad que
# hace que esto sea un piso y no una baja.
gate.n_updates = 50
gate.theta = (1400 - elo.RATING_BASE) / elo.RATING_PER_THETA
arriba = set()
for seed in range(200):
    arriba.add(pick_template(db, gate, random.Random(seed))[0].key)
check(bool(CON_PISO & arriba), f"pasada la barrera vuelven a salir (salieron {sorted(CON_PISO & arriba)})")

# El arranque fijo no toca ninguna: si alguna vez se cambia ONBOARDING por una
# trigonométrica, el piso no la frenaría —ese camino no pasa por pick_template—.
from game.generator import ONBOARDING  # noqa: E402
check(not (CON_PISO & {k for k, _ in ONBOARDING}),
      "el arranque fijo no incluye ninguna con piso")

print()
if FAILURES:
    print(f"{len(FAILURES)} chequeos fallaron:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("todos los chequeos pasaron")
