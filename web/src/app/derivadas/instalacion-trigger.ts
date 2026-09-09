// Cuándo el juego ofrece agregarse a la pantalla de inicio.
//
// Suelto y sin nada de React por el mismo motivo que reclutas-trigger.ts: son
// dos cuentas de módulo y un valor de localStorage, y equivocarse en la cadencia
// no se ve jugando —hay que resolver cinco derivadas para enterarse— sino
// semanas después, en el embudo. Ver web/scripts/check-instalacion-trigger.ts.

import { readInstalarState, readUltimoPedidoAt, saveInstalarState } from "./game-storage"

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
  const { vistas, ultima } = readInstalarState()
  if (vistas >= INSTALAR_MAX) return false
  if (vistas > 0 && totalCorrectas - ultima < INSTALAR_CADA) return false
  return totalCorrectas - readUltimoPedidoAt() >= INSTALAR_SEPARACION
}

/** Anota que se mostró. No toca el cooldown compartido: ver INSTALAR_SEPARACION. */
export function marcarInstalarMostrado(totalCorrectas: number) {
  const { vistas } = readInstalarState()
  saveInstalarState({ vistas: vistas + 1, ultima: totalCorrectas })
}
