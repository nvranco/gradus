"""Primitivas de gráfico en SVG, generadas en el server.

**Por qué SVG a mano y no una librería.** El servicio que sirve esto es la API
de producción: meterle matplotlib o pandas para dibujar seis formas engorda la
imagen y el arranque de un proceso del que dependen los usuarios. Del lado del
cliente, una librería JS obligaría a un CDN (una request más, en un celular con
datos) o a vendorizar un `.min.js` en el repo. Los gráficos son estáticos, así
que el SVG server-side gana en las tres: carga instantánea, imprime a PDF para
el reporte, y cero dependencias.

Todas las funciones devuelven un `str` con un `<svg>` responsive
(`viewBox` + `width:100%`), y toman los colores de variables CSS definidas en
render.py — así el tema vive en un solo lugar.

Coordenadas: el viewBox es un lienzo fijo y el CSS lo escala. Eso significa que
el tamaño del texto que se ve depende del ancho real del contenedor; los valores
de acá están calibrados para tarjetas de ~340 a ~1000 px.
"""
from __future__ import annotations

import math
from html import escape

# Paleta del reporte semanal (docs/reports/gen_report_2026-08-22.py) para que
# el panel y el PDF se lean como la misma familia.
SERIES = ["var(--indigo)", "var(--violet)", "var(--blue)", "var(--brown)", "var(--muted)"]

FONT = "font-family:ui-sans-serif,system-ui,sans-serif"


def esc(s) -> str:
    return escape(str(s), quote=True)


def num(v, suffix: str = "", dec: int = 1) -> str:
    """Formato argentino: coma decimal, sin decimales si es entero.

    `dec` sube la precisión para las magnitudes que viven cerca del cero y que
    con un decimal se verían todas iguales — el coeficiente de viralidad, que
    hoy vale centésimas, se leería «0,0» para cualquier valor real.
    """
    if v is None:
        return "—"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:.{dec}f}".replace(".", ",") + suffix
    return f"{int(v)}{suffix}"


def _svg(w: int, h: int, body: str, extra: str = "", fluid: bool = True) -> str:
    """`fluid=False` fija el tamaño en px. Lo necesita el sparkline: adentro de
    una tarjeta flex, un `width:100%` se estira a todo el espacio sobrante y la
    curva se sale de la tarjeta.

    El `aspect-ratio` explícito no es decorativo. Con `width:100%; height:auto`
    el alto de un SVG depende de su ancho, y el ancho de una celda de grid
    depende del alto de la fila: el navegador resuelve esa circularidad
    midiendo de menos, la tarjeta queda más corta que su contenido y el texto
    de abajo se derrama fuera de la tarjeta (y encima del título siguiente).
    Declarando la proporción, el alto se resuelve sin depender de esa pasada.
    """
    size = (f'width="100%" style="aspect-ratio:{w}/{h};height:auto;'
            if fluid else f'width="{w}" height="{h}" style="')
    return (
        f'<svg viewBox="0 0 {w} {h}" {size}display:block;flex:none;{extra}" '
        f'role="img" xmlns="http://www.w3.org/2000/svg">{body}</svg>'
    )


# Multiplicadores "redondos" del techo del eje. Están elegidos para que el
# valor dividido en 4 (las cuatro líneas de la grilla) siga dando números que
# se leen: 3 → 0,75 · 1,5 · 2,25, y no 3,3333.
_NICE_STEPS = (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 9, 10)


def _nice_max(v: float) -> float:
    """Techo redondo del eje: el múltiplo redondo más chico que deja al valor
    más grande con un poco de aire arriba.

    El orden importa y antes estaba al revés. Iterando el multiplicador por
    fuera y la magnitud por dentro, para 252 se probaba 1 · 1000 antes que
    3 · 100 y el techo salía 1000: la barra ocupaba el 25% del ancho y todo el
    gráfico parecía vacío. La magnitud se calcula, no se busca.
    """
    if v <= 0:
        return 1.0
    target = v * 1.08
    mag = 10 ** math.floor(math.log10(target))
    for step in _NICE_STEPS:
        top = step * mag
        if top >= target:
            return round(top, 10)
    return 10 * mag


# ── Barras horizontales ──────────────────────────────────────────────────────

def hbars(rows: list[dict], *, label: str = "label", value: str = "value",
          note: str = "note", colors: list[str] | None = None,
          suffix: str = "", label_w: int = 190, bar_h: int = 22,
          gap: int = 10, width: int = 760) -> str:
    """Barras horizontales con etiqueta a la izquierda y valor anotado a la
    derecha de la barra. Es la forma del embudo, de las campañas y de casi todo
    ranking: la etiqueta de texto necesita el eje largo."""
    if not rows:
        return _empty()
    top = _nice_max(max((r.get(value) or 0) for r in rows))
    h = len(rows) * (bar_h + gap) + 8
    plot = width - label_w - 96
    out = []
    for i, r in enumerate(rows):
        y = i * (bar_h + gap) + 4
        v = r.get(value) or 0
        bw = max(1.5, plot * v / top)
        color = (colors[i % len(colors)] if colors else SERIES[0])
        out.append(
            f'<text x="{label_w - 10}" y="{y + bar_h * 0.72}" text-anchor="end" '
            f'fill="var(--fg)" font-size="13" {FONT}>{esc(r[label])}</text>'
            f'<rect x="{label_w}" y="{y}" width="{bw:.1f}" height="{bar_h}" rx="3" fill="{color}"/>'
            f'<text x="{label_w + bw + 8:.1f}" y="{y + bar_h * 0.72}" fill="var(--fg)" '
            f'font-size="12.5" font-weight="600" {FONT}>{num(v, suffix)}</text>'
        )
        if r.get(note):
            out.append(
                f'<text x="{label_w + bw + 8 + 11 * len(num(v, suffix)):.1f}" '
                f'y="{y + bar_h * 0.72}" fill="var(--muted)" font-size="11.5" '
                f'{FONT}>{esc(r[note])}</text>')
    return _svg(width, h, "".join(out))


# ── Barras verticales agrupadas ──────────────────────────────────────────────

def vbars(groups: list[str], series: list[dict], *, suffix: str = "",
          width: int = 760, height: int = 210, legend: bool = True) -> str:
    """`series` = [{"label": ..., "values": [...]}, ...], un valor por grupo.

    Es la comparación entre semanas y entre cohortes: pocas categorías, dos o
    tres barras cada una."""
    if not groups or not series:
        return _empty()
    # Con leyenda hace falta una franja propia abajo: si no, se dibuja encima de
    # las etiquetas del eje x.
    pad_l, pad_t = 38, 12
    pad_b = 48 if (legend and len(series) > 1) else 34
    plot_w = width - pad_l - 12
    plot_h = height - pad_b - pad_t
    top = _nice_max(max((v or 0) for s in series for v in s["values"]))
    gw = plot_w / len(groups)
    bw = min(34, gw / (len(series) + 0.7))
    out = [_grid(pad_l, pad_t, plot_w, plot_h, top, suffix)]
    for gi, g in enumerate(groups):
        base = pad_l + gi * gw + (gw - bw * len(series)) / 2
        for si, s in enumerate(series):
            v = s["values"][gi] or 0
            bh = plot_h * v / top
            x = base + si * bw
            out.append(
                f'<rect x="{x:.1f}" y="{pad_t + plot_h - bh:.1f}" width="{bw - 3:.1f}" '
                f'height="{bh:.1f}" rx="3" fill="{SERIES[si % len(SERIES)]}"/>'
                f'<text x="{x + (bw - 3) / 2:.1f}" y="{pad_t + plot_h - bh - 4:.1f}" '
                f'text-anchor="middle" fill="var(--muted)" font-size="10" {FONT}>'
                f'{num(v, suffix)}</text>')
        out.append(
            f'<text x="{pad_l + gi * gw + gw / 2:.1f}" y="{height - pad_b + 16}" '
            f'text-anchor="middle" fill="var(--fg)" font-size="11.5" {FONT}>{esc(g)}</text>')
    if legend and len(series) > 1:
        out.append(_legend([s["label"] for s in series], pad_l, height - 4))
    return _svg(width, height, "".join(out))


# ── Líneas ───────────────────────────────────────────────────────────────────

# Tono de las series monocromas. Es el indigo CLARO y no el de la paleta base:
# sobre el fondo oscuro, el indigo fuerte al 40% de opacidad se compone en un
# gris azulado casi invisible, y la serie más vieja desaparecería.
MONO = "var(--indigo-soft)"

# Piso de la rampa. Medido, no elegido a ojo: compuesto sobre el fondo
# rgb(15,15,30), el indigo claro da 1,9:1 de contraste al 40% y 2,5:1 al 55% —
# por debajo del 3:1 que se le pide a un gráfico que transmite información. A
# 0,65 da 3,06:1 y pasa.
#
# El costo es que entre 0,65 y 1 queda poco rango para distinguir series, así
# que la intensidad no viaja sola: el grosor la acompaña (ver RAMP_WIDTH). Dos
# canales para el mismo orden, que además es lo que hace que se siga leyendo
# impreso en blanco y negro.
RAMP_FLOOR = 0.65
RAMP_WIDTH = (1.7, 2.6)


def ramp(n: int) -> list[float]:
    """Opacidades de menor a mayor, para series del mismo color.

    Cuando las series son el MISMO indicador en momentos distintos (cohortes
    semanales), colores distintos sugieren categorías distintas y hacen que el
    ojo las compare como si fueran cosas separadas. Un solo tono con la vieja
    apagada y la nueva al frente ordena la lectura sola: lo intenso es lo que
    está pasando ahora.
    """
    if n <= 1:
        return [1.0]
    return [round(RAMP_FLOOR + (1 - RAMP_FLOOR) * i / (n - 1), 2) for i in range(n)]


def lines(series: list[dict], x_labels: list[str], *, suffix: str = "%",
          width: int = 760, height: int = 220, y_max: float | None = None,
          band: tuple[float, float] | None = None, legend: bool = True,
          mono: bool = False) -> str:
    """`series` = [{"label": ..., "values": [...], "tips": [...]}]. Los None
    cortan la línea: un hueco es un dato que no existe, y unirlo con una recta
    lo inventaría.

    `tips` (opcional) es un texto por punto que se muestra al pasar el mouse.
    Va como `<title>` dentro del círculo: es el tooltip nativo del navegador,
    así que no necesita JS ni se rompe si el panel se guarda como archivo.

    `weak` (opcional) es un booleano por punto: marca los que se apoyan en poca
    base. Salen con el círculo hueco y la línea punteada. No es lo mismo que un
    None —el dato existe— pero un 0% sobre 4 personas dibujado igual de firme
    que uno sobre 95 hace leer como derrumbe lo que es ruido de la cola.

    `mono=True` dibuja todas las series con el mismo color y una rampa de
    intensidad (ver `ramp`)."""""
    pts_all = [v for s in series for v in s["values"] if v is not None]
    if not pts_all:
        return _empty()
    # Ver vbars: la leyenda necesita su propia franja abajo.
    pad_l, pad_t = 38, 12
    pad_b = 48 if (legend and len(series) > 1) else 32
    plot_w = width - pad_l - 12
    plot_h = height - pad_b - pad_t
    top = y_max or _nice_max(max(pts_all))
    n = max(len(x_labels), 2)
    step = plot_w / (n - 1)

    out = []
    if band:
        y0 = pad_t + plot_h * (1 - band[1] / top)
        y1 = pad_t + plot_h * (1 - band[0] / top)
        out.append(f'<rect x="{pad_l}" y="{y0:.1f}" width="{plot_w:.1f}" '
                   f'height="{max(0, y1 - y0):.1f}" fill="var(--indigo)" opacity="0.10"/>')
    out.append(_grid(pad_l, pad_t, plot_w, plot_h, top, suffix))

    alphas = ramp(len(series)) if mono else [1.0] * len(series)
    for si, s in enumerate(series):
        # `color` propio de la serie: lo usa el desglose por universidad, donde
        # el color NO es un número de orden sino la marca de cada casa de
        # estudios — la misma que el jugador ve en su chip del ranking. Si la
        # serie no trae uno, se cae en la paleta por posición de siempre.
        color = s.get("color") or (MONO if mono else SERIES[si % len(SERIES)])
        op = alphas[si]
        weak = s.get("weak") or [False] * len(s["values"])
        if mono and len(series) > 1:
            lo_w, hi_w = RAMP_WIDTH
            w = round(lo_w + (hi_w - lo_w) * si / (len(series) - 1), 2)
        else:
            w = 2

        # Cada serie va en su propio <g>, indexado por posición: es el
        # gancho que usa el panel para prender/apagar cohortes por CSS
        # (:has() + checkbox) sin depender de JS — ver render.py.
        out.append(f'<g class="cht-s{si}">')
        seg, dots = [], []
        for i, v in enumerate(s["values"]):
            if v is None:
                seg.append(None)
                continue
            x = pad_l + i * step
            y = pad_t + plot_h * (1 - min(v, top) / top)
            seg.append((x, y))
            tip = (s.get("tips") or [None] * len(s["values"]))[i]
            titulo = f"<title>{esc(tip)}</title>" if tip else ""
            # El punto flojo va hueco: relleno del fondo de la tarjeta y borde
            # del color de la serie. Se distingue del sólido de un vistazo sin
            # necesitar leyenda.
            cara = (f'fill="var(--surface)" stroke="{color}" stroke-opacity="{op}" '
                    f'stroke-width="1.4"' if weak[i]
                    else f'fill="{color}" fill-opacity="{op}"')
            # Dos círculos: el visible (chico, para no tapar la línea) y uno
            # transparente y grande que es el blanco del mouse — con r=2.8 hay
            # que acertarle a 5 píxeles y el tooltip no aparece nunca.
            dots.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.8" {cara}/>'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" fill="transparent" '
                f'style="cursor:help">{titulo}</circle>')

        # Dos trazos por serie: el sólido y el punteado. Un punto flojo ensucia
        # los dos segmentos que toca, así que ambos salen punteados — el tramo
        # dibujado con línea llena es exactamente el que se puede leer entero.
        runs: dict[bool, list[str]] = {False: [], True: []}
        prev = None
        for i, p in enumerate(seg):
            if p is None:
                prev = None
                continue
            if prev is not None:
                a = seg[prev]
                runs[weak[i] or weak[prev]].append(
                    f"M{a[0]:.1f},{a[1]:.1f}L{p[0]:.1f},{p[1]:.1f}")
            prev = i
        for dashed, cmds in ((False, runs[False]), (True, runs[True])):
            if not cmds:
                continue
            dash = f' stroke-dasharray="{w * 2:.1f} {w * 1.6:.1f}"' if dashed else ""
            out.append(f'<path d="{"".join(cmds)}" fill="none" stroke="{color}" '
                       f'stroke-opacity="{op}" stroke-width="{w}"{dash} '
                       f'stroke-linejoin="round" stroke-linecap="round"/>')
        out.extend(dots)
        out.append('</g>')

    for i, lab in enumerate(x_labels):
        if len(x_labels) > 10 and i % 2:
            continue
        out.append(f'<text x="{pad_l + i * step:.1f}" y="{height - pad_b + 16}" '
                   f'text-anchor="middle" fill="var(--fg)" font-size="11" {FONT}>{esc(lab)}</text>')
    # La leyenda la decide quien llama, incluso con una sola serie: en un
    # desglose de una sola categoría —una universidad que es la única con base
    # suficiente— la línea sale de color y sin nombre, y no hay forma de saber
    # de quién es.
    if legend and series:
        out.append(_legend([s["label"] for s in series], pad_l, height - 4,
                           mono=mono, colors=[s.get("color") for s in series]))
    return _svg(width, height, "".join(out))


# ── Dispersión ───────────────────────────────────────────────────────────────

def dots(points: list[dict], *, x: str = "x", y: str = "y", group: str = "group",
         x_label: str = "", y_label: str = "", width: int = 760,
         height: int = 240) -> str:
    if not points:
        return _empty()
    pad_l, pad_b, pad_t = 44, 36, 12
    plot_w = width - pad_l - 12
    plot_h = height - pad_b - pad_t
    xmax = _nice_max(max(p[x] for p in points))
    ymax = _nice_max(max(p[y] for p in points))
    groups = sorted({p.get(group, "") for p in points})
    out = [_grid(pad_l, pad_t, plot_w, plot_h, ymax, "")]
    for p in points:
        gi = groups.index(p.get(group, ""))
        cx = pad_l + plot_w * p[x] / xmax
        cy = pad_t + plot_h * (1 - p[y] / ymax)
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" '
                   f'fill="{SERIES[gi % len(SERIES)]}" opacity="0.85"/>')
    if x_label:
        out.append(f'<text x="{pad_l + plot_w / 2:.1f}" y="{height - 4}" text-anchor="middle" '
                   f'fill="var(--muted)" font-size="11" {FONT}>{esc(x_label)}</text>')
    if y_label:
        out.append(f'<text x="12" y="{pad_t + plot_h / 2:.1f}" fill="var(--muted)" '
                   f'font-size="11" transform="rotate(-90 12 {pad_t + plot_h / 2:.1f})" '
                   f'text-anchor="middle" {FONT}>{esc(y_label)}</text>')
    if len(groups) > 1:
        out.append(_legend(groups, pad_l, 10))
    return _svg(width, height, "".join(out))


# ── Barra apilada al 100% ────────────────────────────────────────────────────

def stack(segments: list[dict], *, width: int = 760, height: int = 62,
          colors: list[str] | None = None) -> str:
    """`segments` = [{"label":..., "n":...}]. Para distribuciones de una sola
    variable (los tres valores del canal D, la mezcla de canales)."""
    total = sum(s["n"] for s in segments)
    if not total:
        return _empty("sin respuestas todavía")
    pal = colors or SERIES
    out, x = [], 0.0
    for i, s in enumerate(segments):
        w = width * s["n"] / total
        pct = 100 * s["n"] / total
        out.append(f'<rect x="{x:.1f}" y="0" width="{max(0, w - 2):.1f}" height="26" '
                   f'rx="3" fill="{pal[i % len(pal)]}"/>')
        if pct >= 9:
            out.append(f'<text x="{x + w / 2:.1f}" y="18" text-anchor="middle" fill="#fff" '
                       f'font-size="12" font-weight="600" {FONT}>{pct:.0f}%</text>')
        out.append(
            f'<rect x="{x:.1f}" y="40" width="9" height="9" rx="2" fill="{pal[i % len(pal)]}"/>'
            f'<text x="{x + 13:.1f}" y="48.5" fill="var(--muted)" font-size="11.5" {FONT}>'
            f'{esc(s["label"])} · {s["n"]}</text>')
        x += w
    return _svg(width, height, "".join(out))


# ── Sparkline ────────────────────────────────────────────────────────────────

def spark(values: list[float], *, width: int = 96, height: int = 26) -> str:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    step = width / (len(values) - 1)
    pts = [(i * step, height - 3 - (height - 6) * ((v or lo) - lo) / rng)
           for i, v in enumerate(values)]
    d = " ".join(("M" if i == 0 else "L") + f"{p[0]:.1f},{p[1]:.1f}" for i, p in enumerate(pts))
    last = pts[-1]
    return _svg(width, height,
                f'<path d="{d}" fill="none" stroke="var(--indigo-soft)" stroke-width="1.8" '
                f'stroke-linecap="round" stroke-linejoin="round"/>'
                f'<circle cx="{last[0]:.1f}" cy="{last[1]:.1f}" r="2.6" fill="var(--indigo-soft)"/>',
                fluid=False)


# ── Auxiliares ───────────────────────────────────────────────────────────────

def _grid(pad_l: int, pad_t: int, w: float, h: float, top: float, suffix: str) -> str:
    out = []
    for i in range(5):
        y = pad_t + h * i / 4
        v = top * (4 - i) / 4
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + w:.1f}" y2="{y:.1f}" '
                   f'stroke="var(--grid)" stroke-width="1"/>')
        out.append(f'<text x="{pad_l - 7}" y="{y + 3.5:.1f}" text-anchor="end" '
                   f'fill="var(--muted)" font-size="10" {FONT}>{num(round(v, 1), suffix)}</text>')
    return "".join(out)


def _legend(labels: list[str], x: float, y: float, mono: bool = False,
            colors: list[str | None] | None = None) -> str:
    alphas = ramp(len(labels)) if mono else [1.0] * len(labels)
    out, cx = [], x
    for i, lab in enumerate(labels):
        propio = (colors or [None] * len(labels))[i]
        color = propio or (MONO if mono else SERIES[i % len(SERIES)])
        # Mismo índice que el <g> de la serie en lines(): un toggle apaga la
        # línea y su chip de leyenda con la misma regla CSS.
        out.append(f'<g class="cht-s{i}">'
                   f'<rect x="{cx:.1f}" y="{y - 8}" width="9" height="9" rx="2" '
                   f'fill="{color}" fill-opacity="{alphas[i]}"/>'
                   f'<text x="{cx + 13:.1f}" y="{y}" fill="var(--muted)" font-size="11" '
                   f'{FONT}>{esc(lab)}</text></g>')
        cx += 26 + 6.4 * len(lab)
    return "".join(out)


def _empty(msg: str = "sin datos en esta ventana") -> str:
    return (f'<p style="color:var(--muted);font-size:13px;margin:12px 0;'
            f'font-style:italic">{esc(msg)}</p>')
