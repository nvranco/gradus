// Estado local del minijuego. Todo con try/catch: Safari en modo privado tira
// al escribir y nada de esto puede romper el juego.

const TOKEN_KEY = "intervalo:game:token"
const CAFECITO_LAST_KEY = "intervalo:game:cafecito-last"
const CAFECITO_VISTOS_KEY = "intervalo:game:cafecito-vistos"
const REGISTRO_OFRECIDO_KEY = "intervalo:game:registro-ofrecido"
const INSTALAR_KEY = "intervalo:game:instalar"

// El token del invitado se lee además como STORE REACTIVO (`subscribeGameToken`
// + `getGameTokenSnapshot`, que consume `useGameToken` en UseGamePlayer.ts).
//
// No es una elegancia: `readGameToken()` suelta adentro de un componente es una
// lectura no reactiva, y el React Compiler la memoiza junto al resto de la
// expresión que la contiene. En el `enabled` de la query del jugador eso
// significaba que el gate se evaluaba UNA sola vez —cuando Clerk terminaba de
// cargar, con el invitado todavía sin crear y por lo tanto sin token— y quedaba
// clavado en `false` para el resto de la visita. La query nunca se activaba, las
// invalidaciones de cada respuesta no refrescaban nada, y los tres marcadores de
// la card (ejercicios, racha, elo) se quedaban en cero hasta recargar la página.
//
// Al recargar el token ya estaba guardado en ese único render, así que el bug
// solo se veía en la primera visita: exactamente el síntoma reportado.
//
// El caché de módulo es necesario para `useSyncExternalStore`, que exige que dos
// llamadas seguidas devuelvan el mismo valor mientras nada haya cambiado.
// `undefined` significa "todavía no leído"; `null` es "leído, no hay token".
let tokenCache: string | null | undefined
const tokenListeners = new Set<() => void>()

export function subscribeGameToken(onChange: () => void) {
  tokenListeners.add(onChange)
  return () => {
    tokenListeners.delete(onChange)
  }
}

export function getGameTokenSnapshot(): string | null {
  if (tokenCache === undefined) tokenCache = readGameToken()
  return tokenCache
}

/** En el servidor no hay localStorage y el snapshot tiene que ser estable. */
export function getGameTokenServerSnapshot(): string | null {
  return null
}

export function readGameToken(): string | null {
  if (typeof window === "undefined") return null
  try {
    return window.localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function saveGameToken(token: string) {
  // El caché y el aviso van SIEMPRE, aunque el localStorage falle: en Safari
  // privado el token igual sirve para la sesión en curso, y lo que no puede
  // pasar es que quien lo esté esperando no se entere.
  const changed = tokenCache !== token
  tokenCache = token
  try {
    window.localStorage.setItem(TOKEN_KEY, token)
  } catch {
    // Sin persistencia el juego sigue: se pierde el progreso al recargar.
  }
  if (changed) for (const listener of tokenListeners) listener()
}

/** Borra toda huella local de quién era este jugador: el token de invitado y
 *  los contadores que cuelgan de él (el cooldown de cafecito/reclutar, cuántas
 *  veces se mostró el café, el estado del pedido de instalación y los envíos
 *  recientes del chat).
 *
 *  La usa "Cerrar sesión" en `settings-panel.tsx`: cerrar la sesión de Clerk
 *  sin esto dejaría el token de invitado viejo guardado, y el próximo alta
 *  (`useGamePlayer`) lo mandaría de vuelta tal cual, o sea que "cerrar sesión"
 *  no cerraría nada — solo volvería a mostrar al mismo jugador sin cuenta. */
export function clearGameIdentity() {
  tokenCache = null
  try {
    window.localStorage.removeItem(TOKEN_KEY)
    window.localStorage.removeItem(CAFECITO_LAST_KEY)
    window.localStorage.removeItem(CAFECITO_VISTOS_KEY)
    window.localStorage.removeItem(REGISTRO_OFRECIDO_KEY)
    window.localStorage.removeItem(INSTALAR_KEY)
    window.localStorage.removeItem(CHAT_SENDS_KEY)
  } catch {
    // Nada que limpiar si tampoco se pudo escribir.
  }
  for (const listener of tokenListeners) listener()
}

// Cuándo se le pidió algo a esta persona por última vez: el número de derivada
// resuelta en el que apareció la última diapo que interrumpe para pedir.
//
// Es UNO SOLO para las dos —el cafecito y el reclutamiento— y ahí está su
// gracia. Con un cooldown por diapo, cada una respetaba su propio turno y las
// dos podían caer seguidas: el café por un récord y el reclutamiento por llegar
// a diez, con una sola derivada en el medio. Dos pedidos pegados no son dos
// pedidos, son un peaje. Compartiendo el contador, la regla queda escrita una
// vez y es la que se quiere: un pedido por vez, del tipo que sea.
//
// La clave de localStorage sigue diciendo "cafecito" a propósito: renombrarla
// resetearía el cooldown de todo el mundo, y lo que guarda no cambió.
export function readUltimoPedidoAt(): number {
  if (typeof window === "undefined") return -Infinity
  try {
    const raw = window.localStorage.getItem(CAFECITO_LAST_KEY)
    return raw === null ? -Infinity : Number(raw)
  } catch {
    return -Infinity
  }
}

export function saveUltimoPedidoAt(solvedCount: number) {
  try {
    window.localStorage.setItem(CAFECITO_LAST_KEY, String(solvedCount))
  } catch {}
}

// Cuántas veces le salió sola la diapo del café a esta persona.
//
// Es OTRA cosa que el cooldown de acá arriba, que cuenta derivadas: este cuenta
// APARICIONES, y existe para una sola regla —la primera es siempre «¿Café?»—.
// Ver `elegirTriggerDeCafecito` en cafecito-cta.tsx.
//
// Si no se puede leer, cero: peor caso, alguien ve la copy neutra una vez de
// más, que es exactamente el lado hacia el que conviene errar.
export function readCafecitosVistos(): number {
  if (typeof window === "undefined") return 0
  try {
    const raw = window.localStorage.getItem(CAFECITO_VISTOS_KEY)
    const n = raw === null ? 0 : Number(raw)
    return Number.isFinite(n) && n >= 0 ? n : 0
  } catch {
    return 0
  }
}

export function bumpCafecitosVistos() {
  try {
    window.localStorage.setItem(
      CAFECITO_VISTOS_KEY,
      String(readCafecitosVistos() + 1),
    )
  } catch {}
}

// En qué derivada se le ofreció registrarse por última vez, o -Infinity si
// nunca.
//
// Guardado y no en memoria porque el hito pasó a contarse con las correctas
// ACUMULADAS del jugador (las del servidor) en vez de con las de la pestaña. Con
// un contador de pestaña, recargar reseteaba la cuenta y la oferta no volvía a
// salir; con el del servidor pasa lo contrario —recargar la haría salir de nuevo
// en la primera respuesta, porque la condición ya está cumplida— y una oferta de
// registro en cada recarga es peor que ninguna. Esto es lo que la espacia.
export function readRegistroOfrecidoAt(): number {
  if (typeof window === "undefined") return -Infinity
  try {
    const raw = window.localStorage.getItem(REGISTRO_OFRECIDO_KEY)
    return raw === null ? -Infinity : Number(raw)
  } catch {
    return -Infinity
  }
}

export function marcarRegistroOfrecido(totalCorrectas: number) {
  try {
    window.localStorage.setItem(REGISTRO_OFRECIDO_KEY, String(totalCorrectas))
  } catch {}
}

// Cuándo se mandaron los últimos mensajes al chat, en milisegundos de época —
// los que todavía cuentan para el tope de frecuencia. Guarda una LISTA y no un
// solo instante porque el tope de verdad (limits.por_jugador en el backend,
// hoy tres por minuto) es una ventana corrediza: alcanza con no haber mandado
// tres en el último minuto, no con haber esperado un minuto entero desde el
// anterior.
//
// Clave propia y no la de arriba: aquella cuenta DERIVADAS RESUELTAS y sirve
// para espaciar interrupciones; esta cuenta tiempo de reloj y sirve para que el
// botón sepa que el servidor va a rechazar el próximo mensaje. Son dos cosas
// distintas que se miden en unidades distintas.
//
// Es una copia del tope que manda de verdad: si se pierde —otro navegador,
// borrar datos— no pasa nada, el servidor contesta 429 igual. Lo único que se
// pierde es poder avisarlo antes.
const CHAT_SENDS_KEY = "intervalo:game:chat-sends"

export function readEnviosRecientes(): number[] {
  if (typeof window === "undefined") return []
  try {
    const raw = window.localStorage.getItem(CHAT_SENDS_KEY)
    if (raw === null) return []
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed)
      ? parsed.filter((n): n is number => typeof n === "number")
      : []
  } catch {
    return []
  }
}

/** Registra un envío y de paso poda los que ya salieron de la ventana — así
 *  la lista no crece para siempre en una sesión larga. */
export function registrarEnvio(at: number, ventanaMs: number) {
  try {
    const vigentes = readEnviosRecientes().filter((t) => t > at - ventanaMs)
    vigentes.push(at)
    window.localStorage.setItem(CHAT_SENDS_KEY, JSON.stringify(vigentes))
  } catch {}
}

// Cuántas veces se ofreció agregar el juego a la pantalla de inicio, y en qué
// derivada fue la última.
//
// Son DOS números y no uno porque el pedido se repite: vuelve cada
// `INSTALAR_CADA` derivadas mientras la persona no haya instalado, con un tope
// de `INSTALAR_MAX` apariciones. Con un solo marcador —como el del registro— no
// hay forma de dejar de insistir, y un pedido que vuelve para siempre deja de
// ser un pedido.
//
// Un solo valor JSON en vez de dos claves: se leen y se escriben siempre juntos,
// y dos claves permiten el estado imposible de tener cuenta sin última.
//
// Cualquier cosa ilegible cuenta como "nunca se ofreció". Peor caso, alguien ve
// los pasos de instalación una vez de más — que es el lado hacia el que conviene
// errar, porque el otro es no ofrecérselo nunca a quien sí lo quería.
export type PedidoInstalar = { vistas: number; ultima: number }

const SIN_PEDIR: PedidoInstalar = { vistas: 0, ultima: -Infinity }

export function readInstalarState(): PedidoInstalar {
  if (typeof window === "undefined") return SIN_PEDIR
  try {
    const raw = window.localStorage.getItem(INSTALAR_KEY)
    if (raw === null) return SIN_PEDIR
    const v = JSON.parse(raw) as Partial<PedidoInstalar>
    if (typeof v?.vistas !== "number" || typeof v?.ultima !== "number") {
      return SIN_PEDIR
    }
    return { vistas: v.vistas, ultima: v.ultima }
  } catch {
    return SIN_PEDIR
  }
}

export function saveInstalarState(estado: PedidoInstalar) {
  try {
    window.localStorage.setItem(INSTALAR_KEY, JSON.stringify(estado))
  } catch {}
}
