"use client"

// Prender los recordatorios del juego: permiso del navegador, suscripción y
// preferencia guardada.
//
// Gemelo de `lib/push/UseEnableNotifications.ts` y con las mismas tres etapas,
// pero contra los endpoints del juego. La diferencia no es cosmética: aquel usa
// `useApi()`, que es el cliente autenticado con Clerk, y esto tiene que
// funcionar para un INVITADO —que es entre el 50% y el 95% de cada cohorte del
// juego—. `useGameApi()` manda el token de invitado.
//
// Lo que SÍ se reusa tal cual es la parte que no depende de quién sos:
// `subscribeToPush` (permiso + `pushManager.subscribe` + VAPID), `getTimezone` y
// la lista de horarios. Reescribir eso sería tener dos formas de pedir el mismo
// permiso, y una envejecería.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import posthog from "posthog-js"

import { unwrap } from "@/lib/api/client"
import { getTimezone, isPushSupported, subscribeToPush } from "@/lib/push/register"
import { isStandalone } from "@/lib/platform/detect"

import { useGameApi } from "./UseGameApi"

export { DEFAULT_REMINDER_TIME, REMINDER_TIME_OPTIONS } from "@/lib/push/UseEnableNotifications"

const CLAVE = ["game", "avisos"] as const

export type AvisosDelJuego = {
  enabled: boolean
  time?: string | null
  timezone?: string | null
}

/** ¿Tiene sentido ofrecer los recordatorios en este aparato?
 *
 * Solo dentro de la app instalada, y las dos razones importan:
 *
 *   · en iOS el push web NO EXISTE fuera de la pantalla de inicio, así que el
 *     pedido no tendría nada que ofrecer justo en la mitad del tráfico;
 *   · en Android sí funciona en el navegador, pero el permiso se quema para
 *     siempre si dicen que no, y quemarlo en una pestaña que la persona abrió
 *     hace tres minutos desde WhatsApp es tirar el único canal de retención que
 *     tiene el juego.
 *
 * No es un hook porque lo llama el ladder, que corre dentro de un callback. */
export function puedeOfrecerNotificaciones(): boolean {
  if (typeof window === "undefined") return false
  if (!isStandalone() || !isPushSupported()) return false
  // Ya lo decidió: ni insistir ni volver a preguntar. `denied` es definitivo del
  // lado del navegador —no se puede volver a pedir— y `granted` significa que ya
  // está, así que el pedido solo tiene sentido en `default`.
  return Notification.permission === "default"
}

export function useAvisosDelJuego(activo: boolean) {
  const api = useGameApi()
  return useQuery({
    queryKey: CLAVE,
    queryFn: async () =>
      unwrap(await api.GET("/game/derivemos/notification-settings")) as AvisosDelJuego,
    enabled: activo,
    staleTime: 60_000,
  })
}

export function usePrenderAvisos({ onSuccess }: { onSuccess?: () => void } = {}) {
  const api = useGameApi()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: async (hora: string) => {
      // El orden importa: primero el permiso y la suscripción, y recién después
      // la preferencia. Al revés quedaría `notify_enabled = true` sin ningún
      // navegador al que mandarle, o sea alguien que cree que activó algo.
      const sub = await subscribeToPush()
      unwrap(await api.POST("/game/derivemos/push/subscribe", { body: sub }))
      unwrap(
        await api.PUT("/game/derivemos/notification-settings", {
          body: { enabled: true, time: hora, timezone: getTimezone() },
        }),
      )
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: CLAVE })
      posthog.capture("game_notify_enabled")
      onSuccess?.()
    },
    onError: (error) => {
      posthog.capture("game_notify_enable_failed", { error: String(error) })
    },
  })
}

export function useCambiarHora() {
  const api = useGameApi()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (hora: string) => {
      unwrap(
        await api.PUT("/game/derivemos/notification-settings", {
          body: { enabled: true, time: hora, timezone: getTimezone() },
        }),
      )
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: CLAVE })
    },
  })
}
