// Push service worker for Intervalo daily reminders.
// The backend decides the copy (varied by category, see backend/notification_copy.py) and
// sends it already rendered: an encrypted payload { title, body }. This worker just shows it.

// Sin esto, una versión nueva del SW queda "esperando" hasta que el usuario
// cierre todas las pestañas/instancias de la PWA controladas por la vieja —
// en el peor caso, un push de días después de un deploy todavía se renderiza
// con el código viejo (nos pasó: pushes con payload {title, body} nuevo
// mostrados con el fallback del SW anterior, que esperaba {count}).
self.addEventListener("install", function (event) {
  self.skipWaiting()
})

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim())
})

// URL del backend, pasada como query string al registrar el SW (ver
// register.ts) — un service worker no puede leer process.env.
const API_BASE = new URL(self.location.href).searchParams.get("apiBase")

// Reporta al backend qué pasó con cada push recibido (éxito o fallo de
// decodificación) — no solo los fallos, para poder distinguir "no llegó
// ningún push event" de "llegó pero no se pudo leer". GET simple, sin
// headers custom ni body: es un "simple request" de CORS (sin preflight),
// con keepalive para sobrevivir aunque el SW se cierre apenas termina el
// evento. Estuvimos varias rondas sin que llegara NINGÚN reporte (con la
// versión POST + JSON, que sí dispara preflight) mientras el bug seguía
// pasando, así que esta es la vía con menos superficie de falla posible.
// Best-effort: si igual falla, no bloquea la notificación.
function beacon(params) {
  if (!API_BASE) return Promise.resolve()
  const qs = new URLSearchParams(params).toString()
  return fetch(`${API_BASE}/push/diagnostic?${qs}`, {
    method: "GET",
    keepalive: true,
  }).catch(() => {})
}

// self.registration.pushManager.getSubscription() a veces no resuelve
// (reportes de que ciertas APIs quedan colgadas en un SW despertado por un
// push en iOS) — con timeout para que el beacon salga igual, sin endpoint.
function getSubscriptionEndpoint() {
  return Promise.race([
    self.registration.pushManager
      .getSubscription()
      .then((sub) => (sub ? sub.endpoint : null)),
    new Promise((resolve) => setTimeout(() => resolve(null), 1500)),
  ]).catch(() => null)
}

// Cualquier excepción no capturada en el SW (fuera del try/catch del propio
// handler de push) o promesa rechazada sin catch también se reporta, por si
// la causa real está en otro lado del script.
self.addEventListener("error", function (e) {
  beacon({ event: "sw_error", error: String((e && e.message) || e) })
})
self.addEventListener("unhandledrejection", function (e) {
  beacon({ event: "sw_unhandledrejection", error: String(e.reason) })
})

self.addEventListener("push", function (event) {
  let title = "Intervalo"
  let body = "Tenés repasos pendientes hoy 📚"
  let notificationId = null
  // A dónde lleva el tap y de qué producto es el aviso. Son dos apps instaladas
  // —Intervalo y dx— con dos íconos y dos manifests, así que un aviso del juego
  // que abre "/" manda a la persona al producto equivocado, y un click reportado
  // sin decir de cuál viene marcaría como abierta la fila de otra tabla: los
  // envíos del juego viven en `game_notification_sends`, con su propio espacio
  // de ids. El backend manda los dos campos; sin ellos vale lo de siempre.
  let url = "/"
  let app = ""
  let raw = null
  let decodeError = null

  if (event.data) {
    try {
      raw = event.data.text()
    } catch (e) {
      decodeError = `text() failed: ${e}`
    }
    if (raw != null) {
      try {
        const data = JSON.parse(raw)
        if (data.title) title = data.title
        if (data.body) body = data.body
        if (data.id != null) notificationId = data.id
        if (data.url) url = data.url
        if (data.app) app = data.app
      } catch (e) {
        decodeError = `JSON.parse failed: ${e}`
      }
    }
  } else {
    decodeError = "push event sin event.data"
  }

  if (decodeError) {
    // DEBUG TEMPORAL: ningún reporte de red llegó a aparecer en los logs del
    // backend en varias rondas de prueba en vivo, así que además mostramos
    // el error real directo en el cuerpo de la notificación — cero
    // dependencia de red, no se puede perder en el camino. Revertir apenas
    // tengamos la causa real confirmada.
    body = `[debug] ${decodeError} | raw=${raw != null ? JSON.stringify(raw).slice(0, 120) : "null"}`
  }

  event.waitUntil(
    Promise.all([
      getSubscriptionEndpoint().then((endpoint) =>
        beacon({
          event: decodeError ? "decode_fail" : "ok",
          error: decodeError || "",
          endpoint: endpoint || "",
          raw_len: raw != null ? String(raw.length) : "-1",
          ua: (self.navigator && self.navigator.userAgent) || "",
        }),
      ),
      self.registration.showNotification(title, {
        body,
        icon: "/icons/icon-192.png",
        badge: "/icons/icon-192.png",
        data: { url, app, notificationId },
      }),
    ]),
  )
})

// Reporta el click como "apertura" de la notificación (ver
// backend/push_store.py::mark_notification_opened), para poder analizar
// después tasa de apertura por categoría/variante de copy. Reusa el mismo
// beacon GET+keepalive que el resto del archivo, así que no depende de que
// la app cargue ni de que haya sesión de Clerk.
self.addEventListener("notificationclick", function (event) {
  event.notification.close()
  const url = event.notification.data?.url || "/"
  const notificationId = event.notification.data?.notificationId
  event.waitUntil(
    Promise.all([
      notificationId != null
        ? getSubscriptionEndpoint().then((endpoint) =>
            beacon({
              event: "click",
              notification_id: String(notificationId),
              app: event.notification.data?.app || "",
              endpoint: endpoint || "",
            }),
          )
        : Promise.resolve(),
      self.clients
        .matchAll({ type: "window", includeUncontrolled: true })
        .then((clientList) => {
          for (const client of clientList) {
            if ("focus" in client) {
              client.navigate(url)
              return client.focus()
            }
          }
          return self.clients.openWindow(url)
        }),
    ]),
  )
})
