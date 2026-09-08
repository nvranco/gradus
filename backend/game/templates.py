"""Catálogo de plantillas generadoras de derivadas (v1: solo la tabla básica).

Cada plantilla produce un f(x) aleatorio dentro de su familia, junto con los
errores predecibles de esa familia (derivadas erróneas típicas + feedback).
La derivada esperada NO vive acá: la computa sympy.diff en el generador.

v1 no incluye regla de la cadena ni anidamientos: los argumentos de las
funciones son siempre `x` pelada. La cadena entra en v2 como tiers 6-8
agregando entradas a TEMPLATES — sin tocar esquema ni migraciones (las filas
de game_template_stats se crean lazy con beta seed por tier).

Cada `rng.randint`/`rng.choice` lleva un nombre de ranura (`"n"`, `"k"`...):
es lo que `CyclingRandom` (game/cycler.py) usa para agotar el rango de esa
plantilla antes de repetir un valor. El nombre solo importa DENTRO de una
plantilla — dos plantillas pueden llamar a su ranura "k" sin chocar, el
generador las namespacea por template_key antes de persistirlas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import sympy
from sympy import Integer, Rational, Symbol, cos, exp, log, sin, sqrt, tan

from .cycler import CyclingRandom

x = Symbol("x")

GENERIC_FEEDBACK = "Revisá la tabla de derivadas y probá de nuevo."


@dataclass(frozen=True)
class Generated:
    f: sympy.Expr
    # Derivadas erróneas predecibles + feedback específico (MathText, $ inline).
    common_errors: tuple[tuple[sympy.Expr, str], ...] = ()
    # Override cuando sympy.latex no produce la notación que queremos (log_a).
    prompt_latex: str | None = None


# Piso de Elo para las plantillas con seno, coseno o tangente: por debajo de
# este rating el generador no las sirve, y a partir de acá vuelven a jugar con
# la banda objetivo como cualquier otra.
#
# El motivo por el que esto es un PISO y no una semilla más alta: la β de las
# trigonométricas se desplomó sola. En producción (2026-09-08) `t3_sin` tiene
# β = −3.05 con 12 personas, o sea POR DEBAJO de la semilla de T1, y el
# encogimiento de `elo.effective_beta` solo la levanta hasta −1.99. Subir
# BETA_SEED no alcanza: con 12 personas la semilla pesa 8/20, así que ni
# poniéndola en +1.0 la β creída pasa de −1.4 y el seno se sigue sirviendo
# temprano. El resultado medido es que el 100% de las veces que se sirvió
# `t3_sin` fue por debajo de 1200, con un promedio de 968 y un mínimo de 760.
#
# Un solo número, y sacarlo es borrar el campo de las 8 plantillas de abajo.
PISO_TRIGONOMETRICAS = 1200


@dataclass(frozen=True)
class GameTemplate:
    key: str
    tier: int
    build: Callable[[CyclingRandom], Generated]
    # Feedback cuando el error no matchea ninguno predecible.
    generic_feedback: str = GENERIC_FEEDBACK
    # Rating mínimo del jugador para que el generador pueda servirla. `None` es
    # sin piso, que es el caso de casi todas: el tier y la banda objetivo ya
    # ordenan la dificultad. El piso existe para lo que hay que retrasar por
    # criterio pedagógico y no porque el motor lo crea difícil — ver
    # PISO_TRIGONOMETRICAS.
    min_rating: int | None = None


# ── Errores predecibles compartidos ──────────────────────────────────────────

_FB_POW_NO_DROP = "Al derivar una potencia, el exponente baja multiplicando: revisá $\\left(x^n\\right)'$ en la tabla."
_FB_POW_KEEP_EXP = "El exponente tiene que bajar uno al derivar la potencia."
_FB_LOST_K = "La constante que multiplica se conserva: $(k \\cdot u)' = k \\cdot u'$."
_FB_CONST_STAYS = "Una constante suelta desaparece al derivar: su derivada es $0$."
_FB_PRODUCT_SPLIT = "Derivaste cada factor por separado: la regla del producto es $u'v + uv'$."
_FB_PRODUCT_HALF = "Falta un término: la regla del producto suma $u'v$ y $uv'$."
_FB_QUOTIENT_ORDER = "El orden del numerador importa: arriba va $u'v - uv'$."
_FB_QUOTIENT_SPLIT = "En un cociente no se deriva arriba y abajo por separado: usá la regla del cociente."
_FB_SIN_SIGN = "El signo negativo aparece al derivar $\\cos x$, no $\\operatorname{sen}\\,x$."
_FB_COS_SIGN = "A la derivada de $\\cos x$ le falta el signo negativo."
_FB_EXP_AS_POW = "$e^x$ no es una potencia de $x$: no bajes el exponente."
_FB_AX_NO_LN = "Te falta un factor: la derivada de $a^x$ lleva un $\\ln a$."
_FB_LN_WRONG = "Revisá la tabla: esa no es la derivada de $\\ln x$."
_FB_LOG_NO_LN = "Te falta el $\\ln a$: revisá la derivada de $\\log_a x$ en la tabla."
_FB_RECIP_NO_SIGN = "A la derivada de $1/x$ le falta el signo negativo: es $-1/x^2$."
_FB_RECIP_NO_SQUARE = "Te falta elevar al cuadrado el denominador: la derivada de $1/x$ es $-1/x^2$."
_FB_SQRT_NO_HALF = "Al bajar el exponente $1/2$, ese número queda multiplicando: no te lo saltees."
_FB_TAN_NO_SQUARE = "Te falta elevar al cuadrado: la derivada de $\\tan x$ es $1/\\cos^{2}x$."
_FB_TAN_SIGN = "La derivada de $\\tan x$ no lleva signo negativo."


def _product_errors(u: sympy.Expr, v: sympy.Expr) -> tuple[tuple[sympy.Expr, str], ...]:
    du, dv = sympy.diff(u, x), sympy.diff(v, x)
    return (
        (du * dv, _FB_PRODUCT_SPLIT),
        (du * v, _FB_PRODUCT_HALF),
    )


def _quotient_errors(u: sympy.Expr, v: sympy.Expr) -> tuple[tuple[sympy.Expr, str], ...]:
    du, dv = sympy.diff(u, x), sympy.diff(v, x)
    return (
        ((u * dv - du * v) / v**2, _FB_QUOTIENT_ORDER),
        (du / dv, _FB_QUOTIENT_SPLIT),
    )


# ── Builders por tier ────────────────────────────────────────────────────────

def _t0_const(rng: CyclingRandom) -> Generated:
    k = rng.randint("k", 2, 9)
    return Generated(
        f=Integer(k),
        common_errors=((Integer(k), _FB_CONST_STAYS), (Integer(1), _FB_CONST_STAYS)),
    )


def _t0_x(rng: CyclingRandom) -> Generated:
    return Generated(
        f=x,
        common_errors=((Integer(0), "$x$ no es constante: mirá la segunda fila de la tabla."),),
    )


def _t1_pow(rng: CyclingRandom) -> Generated:
    n = rng.randint("n", 2, 5)
    return Generated(
        f=x**n,
        common_errors=(
            (x ** (n - 1), _FB_POW_NO_DROP),
            (Integer(n) * x**n, _FB_POW_KEEP_EXP),
        ),
    )


def _t1_kx(rng: CyclingRandom) -> Generated:
    k = rng.randint("k", 2, 9)
    return Generated(
        f=Integer(k) * x,
        common_errors=(
            (Integer(0), "Ojo: $kx$ no es una constante."),
            (Integer(k) * x, "Falta derivar: $kx$ cambia cuando cambia $x$."),
        ),
    )


def _t1_kpow(rng: CyclingRandom) -> Generated:
    k = rng.randint("k", 2, 9)
    n = rng.randint("n", 2, 5)
    return Generated(
        f=Integer(k) * x**n,
        common_errors=(
            (Integer(k) * x ** (n - 1), _FB_POW_NO_DROP),
            (Integer(k * n) * x**n, _FB_POW_KEEP_EXP),
            (Integer(n) * x ** (n - 1), _FB_LOST_K),
        ),
    )


def _t1_recip(rng: CyclingRandom) -> Generated:
    k = rng.choice("k", [1, 1, 2, 3, 4, 5])
    f = Integer(k) / x
    return Generated(
        f=f,
        common_errors=(
            (Integer(k) / x**2, _FB_RECIP_NO_SIGN),
            (Integer(-k) / x, _FB_RECIP_NO_SQUARE),
        ),
    )


def _t1_sqrt(rng: CyclingRandom) -> Generated:
    k = rng.choice("k", [1, 1, 2, 3, 4, 5])
    f = Integer(k) * sqrt(x)
    return Generated(
        f=f,
        common_errors=(
            (Integer(k) * Rational(1, 2) * sqrt(x), _FB_POW_KEEP_EXP),
            (Integer(k) / sqrt(x), _FB_SQRT_NO_HALF),
        ),
    )


def _t2_sum2(rng: CyclingRandom) -> Generated:
    a = rng.randint("a", 2, 9)
    b = rng.randint("b", 2, 9)
    n = rng.randint("n", 2, 5)
    f = Integer(a) * x**n + Integer(b) * x
    return Generated(
        f=f,
        common_errors=(
            (Integer(a) * x ** (n - 1) + Integer(b), _FB_POW_NO_DROP),
            (Integer(a * n) * x**n + Integer(b) * x, _FB_POW_KEEP_EXP),
        ),
    )


def _t2_sum3(rng: CyclingRandom) -> Generated:
    a = rng.randint("a", 2, 9)
    b = rng.randint("b", 2, 9)
    c = rng.randint("c", 2, 9)
    n = rng.randint("n", 3, 5)
    m = rng.randint("m", 2, n - 1)
    f = Integer(a) * x**n - Integer(b) * x**m + Integer(c)
    return Generated(
        f=f,
        common_errors=(
            (sympy.diff(f, x) + Integer(c), _FB_CONST_STAYS),
            (Integer(a) * x ** (n - 1) - Integer(b) * x ** (m - 1), _FB_POW_NO_DROP),
        ),
    )


def _t2_pow_plus_const(rng: CyclingRandom) -> Generated:
    n = rng.randint("n", 2, 5)
    k = rng.randint("k", 2, 9)
    f = x**n + Integer(k)
    return Generated(
        f=f,
        common_errors=(
            (sympy.diff(f, x) + Integer(k), _FB_CONST_STAYS),
            (x ** (n - 1), _FB_POW_NO_DROP),
        ),
    )


def _t3_exp(rng: CyclingRandom) -> Generated:
    k = rng.choice("k", [1, 1, 2, 3, 4, 5])
    f = Integer(k) * exp(x)
    return Generated(
        f=f,
        common_errors=(
            (Integer(k) * x * exp(x - 1), _FB_EXP_AS_POW),
            (Integer(0), "$e^x$ no es una constante."),
        ),
    )


def _t3_ln(rng: CyclingRandom) -> Generated:
    k = rng.choice("k", [1, 1, 2, 3, 5])
    f = Integer(k) * log(x)
    return Generated(
        f=f,
        common_errors=(
            (Integer(k) / x**2, _FB_LN_WRONG),
            (Integer(k) * x * log(x), _FB_LN_WRONG),
        ),
    )


def _t3_sin(rng: CyclingRandom) -> Generated:
    k = rng.choice("k", [1, 1, 2, 3, 4])
    f = Integer(k) * sin(x)
    return Generated(f=f, common_errors=((Integer(-k) * cos(x), _FB_SIN_SIGN),))


def _t3_cos(rng: CyclingRandom) -> Generated:
    k = rng.choice("k", [1, 1, 2, 3, 4])
    f = Integer(k) * cos(x)
    return Generated(f=f, common_errors=((Integer(k) * sin(x), _FB_COS_SIGN),))


def _t3_tan(rng: CyclingRandom) -> Generated:
    k = rng.choice("k", [1, 1, 2, 3, 4])
    f = Integer(k) * tan(x)
    return Generated(
        f=f,
        common_errors=(
            (Integer(k) / cos(x), _FB_TAN_NO_SQUARE),
            (Integer(-k) / cos(x) ** 2, _FB_TAN_SIGN),
        ),
    )


def _t3_ax(rng: CyclingRandom) -> Generated:
    a = rng.choice("a", [2, 3, 5])
    f = Integer(a) ** x
    return Generated(
        f=f,
        common_errors=(
            (Integer(a) ** x, _FB_AX_NO_LN),
            (x * Integer(a) ** (x - 1), _FB_EXP_AS_POW),
        ),
    )


def _t3_loga(rng: CyclingRandom) -> Generated:
    a = rng.choice("a", [2, 3, 5, 10])
    # log(x, a) queda internamente como log(x)/log(a); la derivada 1/(x·ln a)
    # sale sola. El latex de sympy para esa forma es ilegible: se escribe a mano.
    f = log(x, a)
    return Generated(
        f=f,
        prompt_latex=rf"\log_{{{a}}}\left(x\right)",
        common_errors=(
            (Rational(1, 1) / x, _FB_LOG_NO_LN),
        ),
    )


def _t3_trig_sum(rng: CyclingRandom) -> Generated:
    a = rng.randint("a", 2, 6)
    b = rng.randint("b", 2, 6)
    f = Integer(a) * sin(x) + Integer(b) * cos(x)
    return Generated(
        f=f,
        common_errors=(
            (Integer(a) * cos(x) + Integer(b) * sin(x), _FB_COS_SIGN),
            (Integer(-a) * cos(x) - Integer(b) * sin(x), _FB_SIN_SIGN),
        ),
    )


def _t3_mix_sum(rng: CyclingRandom) -> Generated:
    k = rng.randint("k", 2, 6)
    n = rng.randint("n", 2, 4)
    f = exp(x) + Integer(k) * x**n
    return Generated(
        f=f,
        common_errors=(
            (x * exp(x - 1) + Integer(k * n) * x ** (n - 1), _FB_EXP_AS_POW),
            (exp(x) + Integer(k) * x ** (n - 1), _FB_POW_NO_DROP),
        ),
    )


def _t4_pow_sin(rng: CyclingRandom) -> Generated:
    n = rng.randint("n", 2, 4)
    u, v = x**n, sin(x)
    return Generated(f=u * v, common_errors=_product_errors(u, v))


def _t4_pow_exp(rng: CyclingRandom) -> Generated:
    n = rng.randint("n", 2, 4)
    u, v = x**n, exp(x)
    return Generated(f=u * v, common_errors=_product_errors(u, v))


def _t4_exp_cos(rng: CyclingRandom) -> Generated:
    u, v = exp(x), cos(x)
    return Generated(f=u * v, common_errors=_product_errors(u, v))


def _t4_pow_ln(rng: CyclingRandom) -> Generated:
    n = rng.randint("n", 2, 4)
    u, v = x**n, log(x)
    return Generated(f=u * v, common_errors=_product_errors(u, v))


def _t4_exp_sin(rng: CyclingRandom) -> Generated:
    u, v = exp(x), sin(x)
    return Generated(f=u * v, common_errors=_product_errors(u, v))


def _t5_sin_over_x(rng: CyclingRandom) -> Generated:
    u, v = sin(x), x
    return Generated(f=u / v, common_errors=_quotient_errors(u, v))


def _t5_pow_over_linear(rng: CyclingRandom) -> Generated:
    n = rng.randint("n", 2, 3)
    k = rng.randint("k", 1, 9)
    u, v = x**n, x + Integer(k)
    return Generated(f=u / v, common_errors=_quotient_errors(u, v))


def _t5_exp_over_pow(rng: CyclingRandom) -> Generated:
    n = rng.randint("n", 1, 3)
    u, v = exp(x), x**n
    return Generated(f=u / v, common_errors=_quotient_errors(u, v))


def _t5_ln_over_x(rng: CyclingRandom) -> Generated:
    u, v = log(x), x
    return Generated(f=u / v, common_errors=_quotient_errors(u, v))


def _t5_linear_over_linear(rng: CyclingRandom) -> Generated:
    a = rng.randint("a", 1, 9)
    k = rng.randint("k", 1, 9)
    if a == k:
        k += 1
    u, v = x + Integer(a), x + Integer(k)
    return Generated(f=u / v, common_errors=_quotient_errors(u, v))


TEMPLATES: tuple[GameTemplate, ...] = (
    GameTemplate("t0_const", 0, _t0_const),
    GameTemplate("t0_x", 0, _t0_x),
    GameTemplate("t1_pow", 1, _t1_pow),
    GameTemplate("t1_kx", 1, _t1_kx),
    GameTemplate("t1_kpow", 1, _t1_kpow),
    GameTemplate("t2_sum2", 2, _t2_sum2),
    GameTemplate("t2_sum3", 2, _t2_sum3),
    GameTemplate("t2_pow_plus_const", 2, _t2_pow_plus_const),
    GameTemplate("t3_exp", 3, _t3_exp),
    GameTemplate("t3_ln", 3, _t3_ln),
    GameTemplate("t3_sin", 3, _t3_sin, min_rating=PISO_TRIGONOMETRICAS),
    GameTemplate("t3_cos", 3, _t3_cos, min_rating=PISO_TRIGONOMETRICAS),
    GameTemplate("t3_ax", 3, _t3_ax),
    GameTemplate("t3_loga", 3, _t3_loga),
    GameTemplate("t3_trig_sum", 3, _t3_trig_sum, min_rating=PISO_TRIGONOMETRICAS),
    GameTemplate("t3_mix_sum", 3, _t3_mix_sum),
    GameTemplate("t4_pow_sin", 4, _t4_pow_sin, "Revisá la regla del producto: $\\left(u \\cdot v\\right)' = u'v + uv'$.", min_rating=PISO_TRIGONOMETRICAS),
    GameTemplate("t4_pow_exp", 4, _t4_pow_exp, "Revisá la regla del producto: $\\left(u \\cdot v\\right)' = u'v + uv'$."),
    GameTemplate("t4_exp_cos", 4, _t4_exp_cos, "Revisá la regla del producto: $\\left(u \\cdot v\\right)' = u'v + uv'$.", min_rating=PISO_TRIGONOMETRICAS),
    GameTemplate("t4_pow_ln", 4, _t4_pow_ln, "Revisá la regla del producto: $\\left(u \\cdot v\\right)' = u'v + uv'$."),
    GameTemplate("t4_exp_sin", 4, _t4_exp_sin, "Revisá la regla del producto: $\\left(u \\cdot v\\right)' = u'v + uv'$.", min_rating=PISO_TRIGONOMETRICAS),
    GameTemplate("t5_sin_over_x", 5, _t5_sin_over_x, "Revisá la regla del cociente.", min_rating=PISO_TRIGONOMETRICAS),
    GameTemplate("t5_pow_over_linear", 5, _t5_pow_over_linear, "Revisá la regla del cociente."),
    GameTemplate("t5_exp_over_pow", 5, _t5_exp_over_pow, "Revisá la regla del cociente."),
    GameTemplate("t5_ln_over_x", 5, _t5_ln_over_x, "Revisá la regla del cociente."),
    GameTemplate("t5_linear_over_linear", 5, _t5_linear_over_linear, "Revisá la regla del cociente."),
    # Agregadas al final a propósito: check_game_explain.py referencia
    # TEMPLATES[16] por posición para t4_pow_sin, e insertar por tier hubiera
    # corrido ese índice sin que el check avisara por qué empezó a fallar.
    GameTemplate("t1_recip", 1, _t1_recip),
    GameTemplate("t1_sqrt", 1, _t1_sqrt),
    GameTemplate("t3_tan", 3, _t3_tan, min_rating=PISO_TRIGONOMETRICAS),
)

TEMPLATE_BY_KEY: dict[str, GameTemplate] = {t.key: t for t in TEMPLATES}


def latex_es(expr: sympy.Expr) -> str:
    """LaTeX de sympy en notación española: `sen` y `ln`.

    La tangente NO se traduce: queda en `\\tan`, que es como la escribe el
    teclado del juego y como la lee MathLive sin ayuda. `tg` sigue aceptándose
    de entrada —el normalizador del front lo convierte— pero ya no se muestra.
    """
    out = sympy.latex(expr, ln_notation=True)
    out = out.replace(r"\sin", r"\operatorname{sen}")
    return out
