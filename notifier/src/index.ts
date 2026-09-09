import "dotenv/config"

import { NodeHttpClient, NodeRuntime } from "@effect/platform-node"
import { Console, Effect, Schedule } from "effect"
import {
  loadConfig,
  runEmailTick,
  runEventTick,
  runGameEventTick,
  runGameTick,
  runSweepTick,
  runTick,
  setupWebPush,
} from "./tick"

const program = Effect.gen(function* () {
  const config = yield* loadConfig
  setupWebPush(config)
  yield* Console.log(
    "notifier started — four push tandas every 15 min (dx first, then Intervalo), emails + abandoned-session sweep every hour",
  )

  // Las cuatro tandas de push, desfasadas de a tres minutos, y el ORDEN es la
  // decisión: las del minijuego van primero.
  //
  // Las cuatro consumen el cupo de la MISMA persona —un jugador registrado
  // reclama contra los contadores de su usuario, ver
  // game/notifications.py :: titular_del_cupo— así que el tope sigue siendo tres
  // avisos por día en total y no tres por producto. Arrancando juntas, la que
  // pierde la carrera decide con un cupo que la otra ya se llevó; corriendo el
  // juego último, dx nunca tendría prioridad aunque el cupo dijera que sí.
  const gameLoop = runGameTick(config).pipe(
    Effect.catch((e) => Console.error("game tick failed:", e)),
    Effect.repeat(Schedule.cron("*/15 * * * *")),
  )
  const gameEventLoop = runGameEventTick(config).pipe(
    Effect.catch((e) => Console.error("game event tick failed:", e)),
    Effect.repeat(Schedule.cron("3-59/15 * * * *")),
  )
  const pushLoop = runTick(config).pipe(
    Effect.catch((e) => Console.error("push tick failed:", e)),
    Effect.repeat(Schedule.cron("6-59/15 * * * *")),
  )
  const eventLoop = runEventTick(config).pipe(
    Effect.catch((e) => Console.error("event tick failed:", e)),
    Effect.repeat(Schedule.cron("9-59/15 * * * *")),
  )
  const emailLoop = runEmailTick(config).pipe(
    Effect.catch((e) => Console.error("email tick failed:", e)),
    Effect.repeat(Schedule.cron("0 * * * *")),
  )
  const sweepLoop = runSweepTick(config).pipe(
    Effect.catch((e) => Console.error("sweep tick failed:", e)),
    Effect.repeat(Schedule.cron("30 * * * *")),
  )

  yield* Effect.all(
    [gameLoop, gameEventLoop, pushLoop, eventLoop, emailLoop, sweepLoop],
    { concurrency: "unbounded" },
  )
})

NodeRuntime.runMain(program.pipe(Effect.provide(NodeHttpClient.layerUndici)))
