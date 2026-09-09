// Cuándo el juego ofrece agregarse a la pantalla de inicio, y cuándo pide
// permiso para mandar recordatorios. Son los dos escalones del mismo camino: en
// iOS el push web NO existe fuera de la app instalada, así que sin el primero el
// segundo no tiene a quién pedirle nada.
//
// Suelto y sin nada de React por el mismo motivo que reclutas-trigger.ts: son
// dos cuentas de módulo y un valor de localStorage, y equivocarse en la cadencia
// no se ve jugando —hay que resolver cinco derivadas para enterarse— sino
// semanas después, en el embudo. Ver web/scripts/check-instalacion-trigger.ts.

import {
  PEDIDO_INSTALAR,
  PEDIDO_NOTIFICACIONES,
  readPedidoState,
  readUltimoPedidoAt,
  savePedidoState,
  saveUltimoPedidoAt,
} from "./game-storage"

// En qué derivada sale la primera vez, y cada cuántas vuelve si no instaló.
//
// El cinco no es redondo, es lo que dice la curva de supervivencia de la cohorte
// más grande medida en producción (52 jugadores): en la derivada 3 sigue el 77%,
// en la 5 el 46%, en la 8 el 33%, en la 10 el 19%, en la 15 el 10% y en la 20 el
// 6%. O sea que cualquier pedido de la 15 para arriba le habla a una de cada
// diez personas y no existe.
//
// Dentro de ese tramo, la 5 es además el punto más CALMO: el abandono en la 3 es
// del 25% y en la 8 del 29%, contra 8,3% en la 5. Se pide justo donde la gente
// no se está yendo.
//
// Vuelve cada 20 —5, 25, 45— y tres veces como máximo. El tope es la mitad del
// asunto: sin él, a quien no quiere instalar se le pregunta para siempre.
export const INSTALAR_PRIMERA = 5
export const INSTALAR_CADA = 20
export const INSTALAR_MAX = 3

// Separación mínima respecto del último pedido del juego (café o reclutas).
//
// Es MENOS que el cooldown compartido de diez, y este pedido tampoco lo consume.
// Es la única excepción y tiene motivo: con diez, cualquier oferta de instalar
// en el tramo 1-10 tapaba la de reclutar, que sale justo en la 10 —o sea que
// reclutar se caía de la derivada 10 (19% de la cohorte) a la 30 (6%)—.
//
// La excepción se sostiene porque instalar es lo único que el juego ofrece que
// NO le pide nada a la persona: ni plata, ni mandarle un mensaje a nadie, ni una
// cuenta, ni un dato. Es una comodidad, se sale con un toque, y es el
// prerrequisito de los recordatorios. Si alguna vez esta diapo pasa a pedir
// algo, tiene que entrar al cooldown compartido como las otras dos.
export const INSTALAR_SEPARACION = 4

/** ¿Toca ofrecer la pantalla de inicio después de esta respuesta?
 *
 * `totalCorrectas` son las ACUMULADAS del jugador, que las manda el servidor.
 * Contándolas en el cliente, cada recarga volvía el contador a cero y el hito no
 * llegaba nunca; ver el comentario largo en hitos-del-juego.ts.
 *
 * Acá NO se mira la plataforma ni si la app ya está instalada: eso lo resuelve
 * quien llama (`puedeOfrecerInstalar` en pedido-instalar.tsx), porque depende
 * del navegador y este módulo tiene que poder correrse solo. */
export function tocaInstalar(totalCorrectas: number): boolean {
  if (totalCorrectas < INSTALAR_PRIMERA) return false
  const { vistas, ultima } = readPedidoState(PEDIDO_INSTALAR)
  if (vistas >= INSTALAR_MAX) return false
  if (vistas > 0 && totalCorrectas - ultima < INSTALAR_CADA) return false
  return totalCorrectas - readUltimoPedidoAt() >= INSTALAR_SEPARACION
}

/** Anota que se mostró. No toca el cooldown compartido: ver INSTALAR_SEPARACION. */
export function marcarInstalarMostrado(totalCorrectas: number) {
  const { vistas } = readPedidoState(PEDIDO_INSTALAR)
  savePedidoState(PEDIDO_INSTALAR, { vistas: vistas + 1, ultima: totalCorrectas })
}

// ── Recordatorios ────────────────────────────────────────────────────────────

// A las TRES derivadas hechas desde la app instalada, y después cada 20, tres
// veces como máximo.
//
// Tan temprano porque la población ya está filtrada: quien llegó hasta acá
// instaló el juego en su teléfono, que es la conversión más dura del embudo. La
// curva de supervivencia de la cohorte más grande dice que en la derivada 3
// sigue el 77% y en la 15 el 10%, así que esperar es perder justo a la persona
// más comprometida.
//
// Y se cuenta desde que instaló, no desde el total: el que instaló en la
// derivada 30 no puede tener que llegar a la 50 para que se le ofrezca lo único
// que puede traerlo de vuelta.
export const NOTIF_PRIMERA = 3
export const NOTIF_CADA = 20
export const NOTIF_MAX = 3

/** ¿Toca ofrecer los recordatorios?
 *
 * `enPwa` son las derivadas hechas desde que la app está instalada, y
 * `totalCorrectas` las acumuladas de siempre: la cadencia se mide en la primera
 * y el peaje en la segunda, porque el cooldown compartido lo llevan las otras
 * diapos y hablan en esa escala.
 *
 * A diferencia del de instalar, este SÍ consume el cooldown compartido: pedir un
 * permiso del sistema es pedir algo, y al lado de un cafecito o de un
 * reclutamiento se lee como dos peajes seguidos.
 *
 * Acá no se mira si el navegador soporta push ni si ya dio el permiso: eso lo
 * resuelve quien llama (`puedeOfrecerNotificaciones`), porque depende del
 * navegador y este módulo tiene que poder correrse solo. */
export function tocaNotificaciones({
  enPwa,
  totalCorrectas,
}: {
  enPwa: number
  totalCorrectas: number
}): boolean {
  if (enPwa < NOTIF_PRIMERA) return false
  const { vistas, ultima } = readPedidoState(PEDIDO_NOTIFICACIONES)
  if (vistas >= NOTIF_MAX) return false
  if (vistas > 0 && enPwa - ultima < NOTIF_CADA) return false
  return totalCorrectas - readUltimoPedidoAt() >= NOTIF_SEPARACION
}

/** El cooldown compartido, que este pedido sí respeta y sí consume. Es el mismo
 *  número que usan el cafecito y reclutas (ver readUltimoPedidoAt). */
export const NOTIF_SEPARACION = 10

export function marcarNotificacionesMostrado({
  enPwa,
  totalCorrectas,
}: {
  enPwa: number
  totalCorrectas: number
}) {
  const { vistas } = readPedidoState(PEDIDO_NOTIFICACIONES)
  savePedidoState(PEDIDO_NOTIFICACIONES, { vistas: vistas + 1, ultima: enPwa })
  saveUltimoPedidoAt(totalCorrectas)
}
