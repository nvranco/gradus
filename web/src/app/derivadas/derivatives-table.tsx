"use client"

// La tabla de derivadas: el dorso del panel del RANKING. Se da vuelta con el
// botón de la cabecera o manteniendo Alt / Option (ver desktop-layout.tsx).
//
// Vivía detrás de la card del ejercicio y se mudó acá por una razón práctica: en
// esa card el dorso tenía que medir lo mismo que el frente —enunciado, campo y
// teclado— y era la cara MÁS ALTA de las dos, así que era la tabla la que fijaba
// el alto de toda la columna izquierda. Del lado del ranking eso no pasa: la
// columna ya es alta y angosta, que es justo la forma de una tabla de catorce
// renglones. Y de paso el ejercicio queda intacto mientras se consulta.
//
// Las filas son las funciones que el juego SIRVE, no una tabla genérica de
// libro — con dos salvedades, no una regla sin excepciones. 1/x, √x y tan x
// (backend/game/stats.py :: ROW_TEMPLATES) NO tiene ninguna plantilla que las
// genere: quedan por completitud de la tabla de bolsillo, y el panel de
// estadísticas (tecla `j`, DerivativesStatsTable acá abajo) las marca con un
// placeholder en vez de inventarles un número. Y al revés, media docena de
// plantillas SÍ se sirven pero no tienen fila propia (la regla de la suma, la
// constante multiplicativa k·u): son combinaciones de lo que ya está arriba,
// y separarlas costaba una fila más para descartar con la vista.
//
// Las dos reglas del final no son decoración: los tiers 4 y 5 son productos y
// cocientes, y sin ellas la tabla no sirve justo donde más se la necesita.

import { Table2 as TableIcon } from "lucide-react"
import { cn } from "@/lib/utils"
import MathText from "@/components/math-text"
import { accuracyColor } from "@/components/metric-card"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { KeyCap } from "./exercise-card"
import { Cara } from "./flip-face"
import { useCartel } from "./game-ranking"
import { useTeclas } from "./teclas"
import type { GameStatsRow } from "./UseGameStats"

// Todas las fórmulas se dibujan en `\displaystyle`, y no es cosmético: en
// modo texto —el de `$...$`— KaTeX arma las fracciones con numerador y
// denominador en `scriptstyle`, o sea al 70%. Una tabla mitad fracciones y
// mitad no terminaba con dos tamaños de letra conviviendo, y las que hay que
// leer con más cuidado eran justo las chiquitas. En display, el numerador y el
// denominador van al mismo cuerpo que el resto.
//
// El precio es alto: una fracción en display ocupa casi el doble. Se paga con
// gusto, porque los renglones NO miden todos lo mismo (ver `Renglon`): cada uno
// pide lo que su fórmula necesita y el bloque se acomoda solo.
const mate = (latex: string) => `$\\displaystyle ${latex}$`

// `slug` identifica la fila para quien mire sus DATOS y no su fórmula: es la
// misma clave que usa game/stats.py :: ROW_TEMPLATES, así que una fila de acá
// y una del payload de /stats se encuentran por `slug`, nunca por posición
// (un array reordenado del lado del server no tendría por qué romper esto).
type Fila = { f: string; d: string; slug: string }

// Una sola tabla de dos columnas. Las dos reglas —producto y cociente— entran
// como dos filas más y no en una tabla aparte: son lo mismo que el resto
// (algo y su derivada) y separarlas obligaba a una segunda cabecera que repetía
// las mismas dos palabras.
// Las fracciones SIMPLES —las que tienen un 1 arriba y un factor abajo— van
// escritas en una sola línea. Apiladas, cada una de esas filas medía 65 px
// contra los 41 de un renglón normal, y seis de ellas eran 150 px que la tabla
// no tiene: en una ventana de las comunes la tabla se cortaba antes de llegar a
// las reglas, que son justo las que más se consultan.
//
// TODAS, incluida la del cociente: con los paréntesis puestos —(u'v − uv')/v²—
// no queda ambigüedad, y a cambio los catorce renglones miden lo mismo. Una
// tabla de renglones parejos se recorre con la vista de un tirón; una con seis
// filas al doble de alto obliga a saltar.
//
// Se pierde algo: `1/x^2` en línea admite leerse mal como `(1/x)^2`. Es un
// precio aceptable en una tabla que se consulta contrarreloj y que ya se está
// mirando con las dos columnas al lado.
const FILAS: Fila[] = [
  { slug: "a", f: "a", d: "0" },
  { slug: "x", f: "x", d: "1" },
  { slug: "x_n", f: "x^n", d: "n\\,x^{n-1}" },
  { slug: "inv_x", f: "1/x", d: "-1/x^{2}" },
  { slug: "sqrt_x", f: "\\sqrt{x}", d: "1/\\left(2\\sqrt{x}\\right)" },
  { slug: "e_x", f: "e^{x}", d: "e^{x}" },
  { slug: "a_x", f: "a^{x}", d: "a^{x}\\ln a" },
  { slug: "ln_x", f: "\\ln x", d: "1/x" },
  { slug: "log_a_x", f: "\\log_a x", d: "1/\\left(x\\ln a\\right)" },
  { slug: "sin_x", f: "\\operatorname{sen} x", d: "\\cos x" },
  { slug: "cos_x", f: "\\cos x", d: "-\\operatorname{sen} x" },
  { slug: "tan_x", f: "\\tan x", d: "1/\\cos^{2} x" },
  { slug: "prod", f: "u \\cdot v", d: "u'v + uv'" },
  { slug: "quot", f: "u/v", d: "\\left(u'v - uv'\\right)/v^{2}" },
]

// El renglón no tiene alto fijo: mide lo que mide su fórmula más un aire
// parejo. Con `\displaystyle` las que tienen fracción son casi el doble de
// altas que un `0` pelado, y forzarlas a todas al mismo alto significaba o
// dejar media tabla con aire de sobra o apretar las fracciones. Repartido así,
// el bloque llena la columna sin huecos y cada fórmula respira lo suyo.
const CELDA = "flex items-center justify-center px-3 py-2 text-center leading-none"

function Renglon({ fila }: { fila: Fila }) {
  return (
    // `grow shrink-0 basis-auto` —o sea `flex: 1 0 auto`—, y ese cero del medio
    // es un arreglo, no un detalle.
    //
    // Crece: reparte el sobrante de la columna entre los catorce renglones en vez
    // de dejarlo todo junto al pie, y proporcional a lo que cada uno mide, así
    // que el que tiene una raíz sigue siendo un poco más alto que el que tiene un
    // 0. Se gana aire sin aplanar las diferencias.
    //
    // Pero NO encoge, y antes sí lo hacía (`flex-auto`, con el 1 en la posición
    // de encoger). Cuando la tabla no entraba —en el teléfono, donde ocupa una
    // pantalla y no media card— los renglones se comprimían hasta que el último
    // quedaba recortado por el `overflow-hidden` del marco, y el scroll no se
    // activaba nunca: el contenido nunca llegaba a ser más alto que su caja.
    // Medido: scrollHeight y clientHeight daban los dos 691.
    //
    // Con shrink en cero el contenido desborda de verdad y el `overflow-y-auto`
    // de arriba hace lo que dice que hace.
    <div className="grid shrink-0 grow basis-auto grid-cols-2 border-t border-white/10">
      <div className={cn(CELDA, "border-r border-white/10")}>
        <MathText text={mate(fila.f)} />
      </div>
      <div className={CELDA}>
        <MathText text={mate(fila.d)} />
      </div>
    </div>
  )
}

export function DerivativesTable() {
  return (
    // Scrollea adentro: en una ventana baja la tabla no tiene que empujar el
    // panel ni salirse por abajo.
    <div className="no-scrollbar flex min-h-0 flex-1 flex-col overflow-y-auto text-[0.95rem]">
      {/* `min-h-full`: cuando sobra alto, la tabla se estira hasta el fondo del
          contenedor y son los renglones los que se lo reparten (ver `Renglon`).
          Cuando falta, no encoge — scrollea, que es lo que corresponde.

          Y para que eso último sea cierto hace falta `shrink-0` acá también. Es
          hijo de un flex column, o sea que por defecto encoge: los renglones ya
          no cedían, pero este marco sí, y su `overflow-hidden` los recortaba
          antes de que el scroller llegara a enterarse de que algo desbordaba.
          Medido con la tabla sin lugar: scrollHeight y clientHeight daban los dos
          439, o sea nada que scrollear, con la última fila cortada igual. */}
      <div className="flex min-h-full shrink-0 flex-col overflow-hidden rounded-md border border-white/10">
        <div className="grid shrink-0 grid-cols-2 bg-white/[0.07] text-center text-[0.7rem] font-medium uppercase tracking-wide text-muted-foreground">
          <div className="border-r border-white/10 py-1">función</div>
          <div className="py-1">derivada</div>
        </div>
        {FILAS.map((fila) => (
          <Renglon key={fila.slug} fila={fila} />
        ))}
      </div>
    </div>
  )
}

// El mismo marco y el mismo criterio de alto que DerivativesTable —crece sin
// encoger, ver el comentario largo de `Renglon`—, pero con dos columnas más
// y sin la de la derivada: acá no se viene a copiar la fórmula (para eso está
// el Alt de siempre), se viene a ver cómo te va a VOS con cada una — la
// velocidad y la efectividad de tus últimos 10 intentos limpios en esa
// familia (backend/game/stats.py :: _personal_accuracy). Es el dorso del
// ranking cuando las estadísticas están abiertas (tecla `j`, ver
// desktop-layout.tsx).
const CELDA_STATS = "flex items-center justify-center px-2 py-2 text-center text-[0.82rem] leading-none tabular-nums"

// Segundos con un decimal, mismo criterio que el panel interno
// (metrics/game_queries.py divide por 1000 antes de mostrar "seg").
function fmtSegundos(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`
}

// El "—" pelado: la explicación de POR QUÉ no hay dato ya no vive acá, vive
// en el `Tip` que envuelve `CeldaConTip` — dos placeholders con el mismo
// aspecto pueden decir cosas distintas (ver `motivoDeLaCelda`).
function Placeholder() {
  return <span className="text-muted-foreground/50">—</span>
}

// El mismo cartelito que ya explica los cuatro contadores de la card del
// ejercicio (exercise-card.tsx :: Tip/Counter), no el `title` del navegador:
// acá hay hasta 28 celdas con algo que explicar y el nativo tarda casi un
// segundo en aparecer y no entra en una sola línea. `group relative` es lo
// que activa el `group-hover` del `Tip` — mismo mecanismo, misma tinta.
function CeldaConTip({
  children,
  title,
  body,
}: {
  children: React.ReactNode
  title: string
  body: string
}) {
  // Popover y no el `Tip` de los cuatro contadores (exercise-card.tsx): ese es
  // CSS puro posicionado `absolute` contra su propio padre, y acá el padre es
  // el marco `overflow-hidden` que redondea las esquinas de la tabla entera
  // (necesario para que las catorce filas no se salgan del borde) — un globo
  // así lo recorta apenas la fila no está pegada arriba de todo. El Popover
  // de Base UI se monta en un portal colgado del `body` (ver el comentario de
  // components/ui/popover.tsx) y por eso no lo agarra ningún `overflow` de
  // ningún ancestro, con o sin scroll. Mismo patrón que ya usa
  // `EloDeUniversidad` (game-ranking.tsx) para el mismo problema.
  const { abierto, setAbierto, gestos } = useCartel()
  return (
    <Popover open={abierto} onOpenChange={setAbierto}>
      <PopoverTrigger
        {...gestos}
        className="inline-flex items-center justify-center outline-none"
        aria-label={title}
      >
        {children}
      </PopoverTrigger>
      <PopoverContent {...gestos} className="text-left text-xs leading-relaxed">
        <p className="font-semibold text-foreground">{title}</p>
        <p className="mt-1 text-muted-foreground">{body}</p>
      </PopoverContent>
    </Popover>
  )
}

/** El cuerpo del tooltip de una celda, con o sin dato — un solo lugar que
 *  decide el texto, para que velocidad y efectividad no puedan desalinearse
 *  entre sí (las dos salen de la MISMA ventana de intentos, ver
 *  game/stats.py :: _personal_accuracy). */
function motivoDeLaCelda(
  noExiste: boolean,
  sample: number,
  hayDato: boolean,
  quien: string,
): string {
  if (noExiste) return "El juego todavía no pide esta derivada."
  if (!hayDato) {
    return sample > 0
      ? `Resolviste ${sample} de este tipo, todavía poco para calcular un dato.`
      : "Todavía no la intentaste."
  }
  return `${quien} de tus últimos ${sample} intentos limpios de este tipo (como mucho, los últimos 10).`
}

function RenglonStats({ fila, datos }: { fila: Fila; datos?: GameStatsRow }) {
  // Sin dato para esta fila —el payload todavía no llegó, o el server nunca
  // manda un slug que el front no conoce— es el mismo placeholder que "no hay
  // plantilla": no hay nada más honesto que decir. `unlock_elo` no se
  // muestra en esta tabla, pero sigue siendo la señal de "esta fila tiene
  // plantilla de verdad". Hoy el server lo manda en las catorce: 1/x, √x y
  // tan x dejaron de ser null cuando ROW_TEMPLATES se puso al día con las
  // plantillas que ya existían (game/stats.py).
  const noExiste = datos === undefined || datos.unlock_elo === null
  const hayVelocidad = !noExiste && datos!.avg_response_ms != null
  const hayEfectividad = !noExiste && datos!.accuracy != null
  return (
    <div className="grid shrink-0 grow basis-auto grid-cols-3 border-t border-white/10">
      <div className={cn(CELDA, "border-r border-white/10")}>
        <MathText text={mate(fila.f)} />
      </div>
      <div className={cn(CELDA_STATS, "border-r border-white/10")}>
        <CeldaConTip
          title="Velocidad"
          body={motivoDeLaCelda(noExiste, datos?.sample ?? 0, hayVelocidad, "Promedio de tiempo")}
        >
          {hayVelocidad ? (
            // Sin heatmap a propósito: no hay "bien" o "mal" en cuánto
            // tardás, y compararlo contra el resto es otra pregunta que esta
            // columna no contesta — ver el pedido que la trajo.
            fmtSegundos(datos!.avg_response_ms!)
          ) : (
            <Placeholder />
          )}
        </CeldaConTip>
      </div>
      <div className={CELDA_STATS}>
        <CeldaConTip
          title="Efectividad"
          body={motivoDeLaCelda(noExiste, datos?.sample ?? 0, hayEfectividad, "Acierto")}
        >
          {hayEfectividad ? (
            <span style={{ color: accuracyColor(datos!.accuracy!) }}>{datos!.accuracy}%</span>
          ) : (
            <Placeholder />
          )}
        </CeldaConTip>
      </div>
    </div>
  )
}

export function DerivativesStatsTable({ rows }: { rows: GameStatsRow[] }) {
  const porSlug = new Map(rows.map((fila) => [fila.slug, fila]))
  return (
    <div className="no-scrollbar flex min-h-0 flex-1 flex-col overflow-y-auto text-[0.95rem]">
      <div className="flex min-h-full shrink-0 flex-col overflow-hidden rounded-md border border-white/10">
        <div className="grid shrink-0 grid-cols-3 bg-white/[0.07] text-center text-[0.65rem] font-medium uppercase tracking-wide text-muted-foreground">
          <div className="border-r border-white/10 py-1">función</div>
          <div className="border-r border-white/10 py-1">velocidad</div>
          <div className="py-1">efectividad</div>
        </div>
        {FILAS.map((fila) => (
          <RenglonStats key={fila.slug} fila={fila} datos={porSlug.get(fila.slug)} />
        ))}
      </div>
    </div>
  )
}

// El botón de la cabecera: el ícono de tabla y el chip de la tecla, sin la
// palabra. El rótulo estaba porque era el único botón de esa esquina que hacía
// algo DENTRO del juego —y que además cuesta Elo— pero con el chip `alt` al
// lado ya se entiende que abre algo, y el nombre lo dice el `aria-label` para
// quien lo necesita. Tres botones con texto en una esquina de 440 px se pisaban
// entre sí.
export function TableButton({
  open,
  onToggle,
  keyboard = true,
  className,
}: {
  open: boolean
  onToggle: () => void
  // El chip de la tecla solo donde hay tecla. En el teléfono se toca, y un
  // "alt" impreso al lado sería prometer un atajo que no existe.
  keyboard?: boolean
  className?: string
}) {
  const teclas = useTeclas()
  return (
    <button
      type="button"
      aria-label={open ? "Volver al ranking" : "Ver la tabla de derivadas"}
      aria-pressed={open}
      onClick={onToggle}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-sm transition-colors",
        open
          ? "border-foreground/40 bg-accent text-foreground"
          : "border-border text-muted-foreground hover:bg-accent",
        className,
      )}
    >
      <TableIcon size={15} />
      {keyboard && <KeyCap className="ml-0">{teclas.alt}</KeyCap>}
    </button>
  )
}

// El cambio de cara: la de adelante se apaga y la de atrás se enciende en el
// mismo lugar. Es el mismo fundido con el que el juego cambia de pantalla —de
// hecho son sus mismos números (FUNDIDO, slide-flip.tsx)—, y eso es a propósito:
// dar vuelta una card y pasar a otra pantalla tienen que sentirse la misma
// familia de gesto.
//
// Antes esto era un VOLTEO en 3D: las dos caras vivían superpuestas dentro de un
// contenedor que giraba, la de atrás nacía ya rotada 180° y `backfaceVisibility`
// evitaba verla espejada en el camino. Se fue junto con el volteo de las
// pantallas, y con él se fueron sus dos condiciones frágiles: la `perspective`
// tenía que ir en un contenedor APARTE del que rota, y cualquier ancestro con
// overflow, filter u opacity aplanaba el contexto 3D y dejaba el giro plano.
// También se había probado una PERSIANA (translateY, sin 3D) y se descartó:
// mover la caja de lugar para cambiar lo que dice adentro era un gesto más
// grande que el cambio que anunciaba. El fundido no mueve nada; solo enciende y
// apaga.

export function FlipCard({
  flipped,
  front,
  back,
  className,
}: {
  flipped: boolean
  front: React.ReactNode
  back: React.ReactNode
  className?: string
}) {
  return (
    <div className={cn("relative", className)}>
      <Cara visible={!flipped} className="flex h-full w-full flex-col">
        {front}
      </Cara>
      <Cara visible={flipped} className="absolute inset-0 flex flex-col">
        {back}
      </Cara>
    </div>
  )
}

// `Cara` se mudó a flip-face.tsx: la usa también exercise-card.tsx ::
// AnswerField, para el campo que se convierte en el botón del «¿Por qué?», y
// puesta acá esa importación hubiera cerrado un círculo (este archivo ya
// importa `KeyCap` de exercise-card.tsx).
