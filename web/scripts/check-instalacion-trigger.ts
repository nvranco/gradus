// Chequeo del disparador de la diapo que ofrece la pantalla de inicio.
//
// Corre con: bun run check:instalacion
//
// Lo mismo que su gemelo de reclutas: es una cuenta de módulo y un valor de
// localStorage, y equivocarse no se ve jugando —hay que resolver cinco derivadas
// para enterarse— sino semanas después, en el embudo.
//
// Lo que importa comprobar son las tres cosas que la hacen soportable: que caiga
// en los números elegidos, que DEJE de insistir, y que no le corra el turno a
// los otros dos pedidos. Ese último es el que motivó la excepción del cooldown
// (ver INSTALAR_SEPARACION) y el que se rompería sin que nadie lo note.

import {
  readUltimoPedidoAt,
  saveUltimoPedidoAt,
} from "../src/app/derivadas/game-storage"
import {
  INSTALAR_CADA,
  INSTALAR_MAX,
  INSTALAR_PRIMERA,
  INSTALAR_SEPARACION,
  NOTIF_CADA,
  NOTIF_MAX,
  NOTIF_PRIMERA,
  NOTIF_SEPARACION,
  marcarInstalarMostrado,
  marcarNotificacionesMostrado,
  tocaInstalar,
  tocaNotificaciones,
} from "../src/app/derivadas/instalacion-trigger"
import {
  RECLUTAS_CADA,
  RECLUTAS_RESTO,
  marcarReclutasMostrado,
  tocaReclutar,
} from "../src/app/derivadas/reclutas-trigger"
import { CAFECITO_COOLDOWN, CAFECITO_EVERY } from "../src/app/derivadas/cafecito-cta"

// `game-storage` habla con localStorage y esto corre en bun, sin navegador.
const guardado = new Map<string, string>()
Object.assign(globalThis, {
  window: {
    localStorage: {
      getItem: (k: string) => guardado.get(k) ?? null,
      setItem: (k: string, v: string) => void guardado.set(k, v),
      removeItem: (k: string) => void guardado.delete(k),
    },
  },
})

let fallos = 0
function check(ok: boolean, label: string) {
  console.log(`  [${ok ? "ok" : "FAIL"}] ${label}`)
  if (!ok) fallos++
}

function limpio() {
  guardado.clear()
}

// El café con los mismos valores que usa el juego, para poder simular el ladder
// entero y no solo la parte que toca este archivo.
function tocaCafecito(total: number, esRecord = false): boolean {
  const porHito = total > 0 && total % CAFECITO_EVERY === 0
  if (!porHito && !esRecord) return false
  return total - readUltimoPedidoAt() >= CAFECITO_COOLDOWN
}

console.log(
  `valores: instalar en ${INSTALAR_PRIMERA} y cada ${INSTALAR_CADA} ` +
    `(máximo ${INSTALAR_MAX}, separación ${INSTALAR_SEPARACION}), ` +
    `reclutas cada ${RECLUTAS_CADA} resto ${RECLUTAS_RESTO}, café cada ${CAFECITO_EVERY}`,
)

console.log("1. cae en los números elegidos y deja de insistir")
limpio()
const salen: number[] = []
for (let n = 1; n <= 200; n++) {
  if (tocaInstalar(n)) {
    salen.push(n)
    marcarInstalarMostrado(n)
  }
}
check(salen.length === INSTALAR_MAX, `sale ${INSTALAR_MAX} veces y no más (${salen.length})`)
check(salen[0] === INSTALAR_PRIMERA, `la primera es en la ${INSTALAR_PRIMERA} (${salen[0]})`)
check(
  salen.every((n, i) => i === 0 || n - salen[i - 1] === INSTALAR_CADA),
  `separadas por ${INSTALAR_CADA} (${salen.join(", ")})`,
)

console.log("2. antes de la primera no sale, y en cero tampoco")
limpio()
check(!tocaInstalar(0), "no sale en cero")
check(!tocaInstalar(-3), "ni con un número negativo")
check(
  Array.from({ length: INSTALAR_PRIMERA - 1 }, (_, i) => !tocaInstalar(i + 1)).every(Boolean),
  `no sale antes de la ${INSTALAR_PRIMERA}`,
)

console.log("3. no le corre el turno a reclutar ni al café")
// El ladder entero, en el orden real de mobile-flow: instalar va primero, pero
// sin consumir el cooldown compartido. Es exactamente la propiedad que la
// excepción tiene que garantizar — si instalar tomara el cooldown, el pedido de
// reclutar se caería de la derivada 10 a la 30.
limpio()
const pedidos: { n: number; que: string }[] = []
for (let n = 1; n <= 200; n++) {
  // Un récord cada 23 respuestas, para que el café también salga fuera de sus
  // múltiplos y el ladder se parezca al de una partida de verdad.
  const esRecord = n % 23 === 0
  if (tocaInstalar(n)) {
    marcarInstalarMostrado(n)
    pedidos.push({ n, que: "instalar" })
    continue
  }
  if (tocaCafecito(n, esRecord)) {
    saveUltimoPedidoAt(n)
    pedidos.push({ n, que: "cafecito" })
    continue
  }
  if (tocaReclutar(n)) {
    marcarReclutasMostrado(n)
    pedidos.push({ n, que: "reclutas" })
  }
}
const reclutas = pedidos.filter((p) => p.que === "reclutas").map((p) => p.n)
check(
  reclutas[0] === RECLUTAS_RESTO,
  `reclutar sigue saliendo en la ${RECLUTAS_RESTO} (${reclutas[0]})`,
)
check(
  pedidos.filter((p) => p.que === "cafecito").length > 0,
  "el café sigue saliendo",
)
check(
  pedidos.filter((p) => p.que === "instalar").length === INSTALAR_MAX,
  "y instalar sale sus tres veces igual",
)

console.log("4. nunca dos pedidos pegados")
// La separación de instalar es MENOR que el cooldown compartido a propósito,
// así que la vara acá es la suya: ningún par de pedidos consecutivos puede caer
// a menos de `INSTALAR_SEPARACION` derivadas.
let minimo = Infinity
for (let i = 1; i < pedidos.length; i++) {
  minimo = Math.min(minimo, pedidos[i]!.n - pedidos[i - 1]!.n)
}
check(
  minimo >= INSTALAR_SEPARACION,
  `el hueco más chico entre dos pedidos es ${minimo} (mínimo ${INSTALAR_SEPARACION})`,
)
check(
  new Set(pedidos.map((p) => p.n)).size === pedidos.length,
  "y nunca dos en la misma respuesta",
)

console.log("5. los recordatorios se cuentan desde que instaló")
limpio()
// Alguien que instaló en la derivada 30: la cadencia arranca ahí y no en el
// total, así que el pedido le llega en la 33 y no cuando el total llegue a 3.
const DESDE = 30
const conRecordatorios: number[] = []
for (let n = DESDE; n <= DESDE + 100; n++) {
  if (tocaNotificaciones({ enPwa: n - DESDE, totalCorrectas: n })) {
    conRecordatorios.push(n - DESDE)
    marcarNotificacionesMostrado({ enPwa: n - DESDE, totalCorrectas: n })
  }
}
check(
  conRecordatorios[0] === NOTIF_PRIMERA,
  `el primero llega a las ${NOTIF_PRIMERA} derivadas en la app (${conRecordatorios[0]})`,
)
check(
  conRecordatorios.length === NOTIF_MAX,
  `sale ${NOTIF_MAX} veces y no más (${conRecordatorios.length})`,
)
check(
  conRecordatorios.every((n, i) => i === 0 || n - conRecordatorios[i - 1]! === NOTIF_CADA),
  `separados por ${NOTIF_CADA} (${conRecordatorios.join(", ")})`,
)
limpio()
check(
  !tocaNotificaciones({ enPwa: NOTIF_PRIMERA - 1, totalCorrectas: 100 }),
  "y no sale antes, por más total que tenga",
)

console.log("6. los recordatorios SÍ respetan el peaje compartido")
// Al revés que el de instalar: pedir un permiso del sistema es pedir algo, y al
// lado de un cafecito se lee como dos peajes seguidos.
limpio()
saveUltimoPedidoAt(100)
check(
  !tocaNotificaciones({ enPwa: 50, totalCorrectas: 100 + NOTIF_SEPARACION - 1 }),
  "pegado a otro pedido no sale",
)
check(
  tocaNotificaciones({ enPwa: 50, totalCorrectas: 100 + NOTIF_SEPARACION }),
  "y con el cooldown cumplido sí",
)
check(
  readUltimoPedidoAt() === 100,
  "mientras no lo muestre, no consume el turno de nadie",
)
marcarNotificacionesMostrado({ enPwa: 50, totalCorrectas: 120 })
check(readUltimoPedidoAt() === 120, "y al mostrarse sí lo consume")

console.log("7. un localStorage roto no lo apaga")
// Safari en modo privado tira al escribir. Peor caso aceptado: se ofrece de más,
// nunca de menos — el otro lado sería no ofrecérselo nunca a quien sí lo quería.
limpio()
const setItem = (globalThis as { window: { localStorage: { setItem: unknown } } }).window
  .localStorage.setItem
;(globalThis as unknown as { window: { localStorage: { setItem: () => void } } }).window.localStorage.setItem =
  () => {
    throw new Error("QuotaExceeded")
  }
marcarInstalarMostrado(INSTALAR_PRIMERA)
check(tocaInstalar(INSTALAR_PRIMERA), "sigue ofreciéndose si no se pudo anotar")
;(globalThis as unknown as { window: { localStorage: { setItem: unknown } } }).window.localStorage.setItem =
  setItem

console.log(fallos === 0 ? "\ntodos los chequeos pasaron" : `\n${fallos} fallos`)
process.exit(fallos === 0 ? 0 : 1)
