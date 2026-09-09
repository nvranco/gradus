import { Config, Console, Effect } from "effect"
import {
  HttpClient,
  HttpClientRequest,
  HttpClientResponse,
} from "effect/unstable/http"
import webpush from "web-push"

export interface NotifierConfig {
  apiBaseUrl: string
  secret: string
  vapid: { publicKey: string; privateKey: string; subject: string }
}

interface PushSub {
  id: number
  endpoint: string
  p256dh: string
  auth: string
}

interface DueNotification {
  title: string
  body: string
  notification_id: number
  subscriptions: PushSub[]
  // A dónde lleva el tap. Solo lo manda el minijuego, que se instala como app
  // aparte: sin esto el service worker abre "/" y el aviso de dx termina en la
  // home de Intervalo.
  url?: string
  // Los dos que solo trae Intervalo. Acá no se usan —el envío solo necesita
  // título, cuerpo, id y suscripciones— y por eso son opcionales: los avisos del
  // juego pueden ir para un invitado, que no tiene `user_id`.
  user_id?: number
  pending_count?: number
}

export const loadConfig: Effect.Effect<NotifierConfig, Error> = Effect.gen(
  function* () {
    return {
      apiBaseUrl: yield* Config.string("API_BASE_URL"),
      secret: yield* Config.string("INTERNAL_API_SECRET"),
      vapid: {
        publicKey: yield* Config.string("VAPID_PUBLIC_KEY"),
        privateKey: yield* Config.string("VAPID_PRIVATE_KEY"),
        subject: yield* Config.string("VAPID_SUBJECT"),
      },
    }
  },
).pipe(Effect.mapError((e) => new Error(`missing config: ${e}`)))

/** Configure web-push's VAPID details once, before sending. */
export function setupWebPush(config: NotifierConfig): void {
  webpush.setVapidDetails(
    config.vapid.subject,
    config.vapid.publicKey,
    config.vapid.privateKey,
  )
}

/** Resultado de un envío: la suscripción a purgar si murió (404/410), y el
 * estado a guardar en notification_sends. El backend crea esa fila al elegir el
 * copy, o sea antes de que se intente mandar, así que si el estado no vuelve
 * acá un envío que nunca salió queda igual que uno exitoso. */
interface SendOutcome {
  deadSubscriptionId: number | null
  notificationId: number
  status: string
}

/** Send one push; resolves with the outcome to report back. */
const sendPush = (
  sub: PushSub,
  payload: {
    title: string
    body: string
    notificationId: number
    url?: string
    app?: string
  },
): Effect.Effect<SendOutcome> =>
  Effect.tryPromise({
    try: () =>
      webpush.sendNotification(
        { endpoint: sub.endpoint, keys: { p256dh: sub.p256dh, auth: sub.auth } },
        JSON.stringify({
          title: payload.title,
          body: payload.body,
          id: payload.notificationId,
          // `app` viaja de vuelta en el beacon del click: los envíos del juego
          // están en otra tabla, con otro espacio de ids, y sin decir de cuál
          // viene el click marcaría abierta la fila equivocada.
          ...(payload.url ? { url: payload.url } : {}),
          ...(payload.app ? { app: payload.app } : {}),
        }),
        { TTL: 86400 },
      ),
    catch: (error) => error,
  }).pipe(
    Effect.as<SendOutcome>({
      deadSubscriptionId: null,
      notificationId: payload.notificationId,
      status: "ok",
    }),
    Effect.catch((error) => {
      const status = (error as { statusCode?: number })?.statusCode
      const dead = status === 404 || status === 410
      return Console.warn(
        `push failed sub=${sub.id} status=${status ?? "?"}${dead ? " (pruning)" : ""}`,
      ).pipe(
        Effect.as<SendOutcome>({
          deadSubscriptionId: dead ? sub.id : null,
          notificationId: payload.notificationId,
          status: status ? `error_${status}` : "error",
        }),
      )
    }),
  )

/** Una tanda de pushes: pide los que corresponde mandar, los manda, reporta la
 * entrega y limpia las suscripciones muertas.
 *
 * El endpoint es un parámetro porque hay dos que devuelven exactamente la misma
 * forma: `/due` (la notificación del horario elegido) y `/events` (los avisos de
 * cafecito y reclutas). Lo que cambia es POR QUÉ salen, y eso ya lo decidió el
 * backend; de este lado no hay ninguna diferencia que justifique dos copias del
 * envío, el reporte de entrega y el prune. */
const correrTanda = (
  config: NotifierConfig,
  {
    ruta,
    etiqueta,
    force,
    // Las rutas de reporte son parámetros por lo mismo que la de origen: el
    // minijuego guarda sus envíos y sus suscripciones en tablas propias, así que
    // un `notification_id` puede referirse a dos filas distintas según de qué
    // tanda venga. Los valores por defecto son los de Intervalo.
    rutaDelivery = "/internal/push/delivery",
    rutaPrune = "/internal/push/prune",
    app,
  }: {
    ruta: string
    etiqueta: string
    force?: boolean
    rutaDelivery?: string
    rutaPrune?: string
    app?: string
  },
): Effect.Effect<void, Error, HttpClient.HttpClient> =>
  Effect.gen(function* () {
    const client = yield* HttpClient.HttpClient

    const url = `${config.apiBaseUrl}${ruta}${force ? "?force=true" : ""}`
    const dueRes = yield* client
      .execute(
        HttpClientRequest.get(url).pipe(
          HttpClientRequest.setHeader("X-Internal-Secret", config.secret),
        ),
      )
      .pipe(Effect.flatMap(HttpClientResponse.filterStatusOk))
    const users = (yield* dueRes.json) as unknown as DueNotification[]

    const jobs = users.flatMap((u) =>
      u.subscriptions.map((sub) => ({
        sub,
        title: u.title,
        body: u.body,
        notificationId: u.notification_id,
        url: u.url,
      })),
    )
    yield* Console.log(
      `${etiqueta}: ${users.length} user(s) due, ${jobs.length} push(es) to send`,
    )
    if (jobs.length === 0) return

    const results = yield* Effect.forEach(
      jobs,
      (job) =>
        sendPush(job.sub, {
          title: job.title,
          body: job.body,
          notificationId: job.notificationId,
          url: job.url,
          app,
        }),
      { concurrency: 5 },
    )
    // El reporte de entrega va antes del prune: si el tick se cae a la mitad,
    // preferimos haber guardado por qué falló antes que haber limpiado la
    // suscripción y perder el motivo.
    yield* client
      .execute(
        HttpClientRequest.post(`${config.apiBaseUrl}${rutaDelivery}`).pipe(
          HttpClientRequest.setHeader("X-Internal-Secret", config.secret),
          HttpClientRequest.bodyJsonUnsafe({
            results: results.map((r) => ({
              notification_id: r.notificationId,
              status: r.status,
            })),
          }),
        ),
      )
      .pipe(Effect.flatMap(HttpClientResponse.filterStatusOk))
    const okCount = results.filter((r) => r.status === "ok").length
    yield* Console.log(
      `delivery: ${okCount}/${results.length} ok`,
    )

    const deadIds = results
      .map((r) => r.deadSubscriptionId)
      .filter((id): id is number => id !== null)

    if (deadIds.length > 0) {
      yield* client.execute(
        HttpClientRequest.post(`${config.apiBaseUrl}${rutaPrune}`).pipe(
          HttpClientRequest.setHeader("X-Internal-Secret", config.secret),
          HttpClientRequest.bodyJsonUnsafe({ subscription_ids: deadIds }),
        ),
      )
      yield* Console.log(`pruned ${deadIds.length} dead subscription(s)`)
    }
  }).pipe(Effect.mapError((e) => (e instanceof Error ? e : new Error(String(e)))))

/** La notificación diaria: sale porque llegó el horario que la persona eligió. */
export const runTick = (
  config: NotifierConfig,
  options: { force?: boolean } = {},
): Effect.Effect<void, Error, HttpClient.HttpClient> =>
  correrTanda(config, {
    ruta: "/internal/notifications/due",
    etiqueta: "tick",
    force: options.force,
  })

/** Los avisos de evento: salen porque pasó algo —un cafecito para tu
 * universidad, un recluta que empezó a generarte XP—. Tienen cupo propio y
 * disparador propio, así que van en su propia tanda: un fallo del endpoint de
 * eventos no puede dejar sin mandar la notificación del horario. */
export const runEventTick = (
  config: NotifierConfig,
  options: { force?: boolean } = {},
): Effect.Effect<void, Error, HttpClient.HttpClient> =>
  correrTanda(config, {
    ruta: "/internal/notifications/events",
    etiqueta: "event tick",
    force: options.force,
  })

// Las rutas de reporte del minijuego, para no repetir el literal en las dos
// tandas: sus envíos y sus suscripciones viven en tablas propias.
const RUTAS_DEL_JUEGO = {
  rutaDelivery: "/internal/push/game-delivery",
  rutaPrune: "/internal/push/game-prune",
  app: "dx",
} as const

/** El aviso programado del minijuego. */
export const runGameTick = (
  config: NotifierConfig,
  options: { force?: boolean } = {},
): Effect.Effect<void, Error, HttpClient.HttpClient> =>
  correrTanda(config, {
    ruta: "/internal/notifications/game",
    etiqueta: "game tick",
    force: options.force,
    ...RUTAS_DEL_JUEGO,
  })

/** Los avisos reactivos del minijuego: cafecito, reclutas, ranking, universidad. */
export const runGameEventTick = (
  config: NotifierConfig,
  options: { force?: boolean } = {},
): Effect.Effect<void, Error, HttpClient.HttpClient> =>
  correrTanda(config, {
    ruta: "/internal/notifications/game-events",
    etiqueta: "game event tick",
    force: options.force,
    ...RUTAS_DEL_JUEGO,
  })

interface EmailRunResult {
  bounce_sent: number
  winback_sent: number
  streak_tier_sent: number
  cafecito_efecto_sent: number
  reclutas_sent: number
}

/** One scheduler tick for lifecycle emails: the backend resolves recipients
 * and sends via Resend itself, so this just triggers the batch. */
export const runEmailTick = (
  config: NotifierConfig,
): Effect.Effect<void, Error, HttpClient.HttpClient> =>
  Effect.gen(function* () {
    const client = yield* HttpClient.HttpClient

    const res = yield* client
      .execute(
        HttpClientRequest.post(`${config.apiBaseUrl}/internal/emails/run`).pipe(
          HttpClientRequest.setHeader("X-Internal-Secret", config.secret),
        ),
      )
      .pipe(Effect.flatMap(HttpClientResponse.filterStatusOk))
    const result = (yield* res.json) as unknown as EmailRunResult
    yield* Console.log(
      `email tick: ${result.bounce_sent} bounce, ${result.winback_sent} win-back, ` +
        `${result.streak_tier_sent} streak-tier, ${result.cafecito_efecto_sent} cafecito, ` +
        `${result.reclutas_sent} reclutas sent`,
    )
  }).pipe(Effect.mapError((e) => (e instanceof Error ? e : new Error(String(e)))))

interface SweepAbandonedResult {
  marked: number
}

/** One scheduler tick to close out sessions the user never finished. Abandonment
 * can't be detected when it happens — nobody reports leaving — so it is swept by
 * elapsed time instead. See session_store.sweep_abandoned_sessions. */
export const runSweepTick = (
  config: NotifierConfig,
): Effect.Effect<void, Error, HttpClient.HttpClient> =>
  Effect.gen(function* () {
    const client = yield* HttpClient.HttpClient

    // filterStatusOk, no solo parsear: sin esto un 404 o un 401 devuelven un body
    // JSON válido pero con otra forma, y el tick loguea "undefined session(s)"
    // como si hubiera corrido bien. Pasó en el primer deploy, cuando el notifier
    // arrancó unos segundos antes que el backend.
    const res = yield* client
      .execute(
        HttpClientRequest.post(
          `${config.apiBaseUrl}/internal/sessions/sweep-abandoned`,
        ).pipe(HttpClientRequest.setHeader("X-Internal-Secret", config.secret)),
      )
      .pipe(Effect.flatMap(HttpClientResponse.filterStatusOk))
    const result = (yield* res.json) as unknown as SweepAbandonedResult
    yield* Console.log(`sweep tick: ${result.marked} session(s) marked abandoned`)
  }).pipe(Effect.mapError((e) => (e instanceof Error ? e : new Error(String(e)))))
