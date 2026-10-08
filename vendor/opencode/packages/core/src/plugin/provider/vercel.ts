import { Effect } from "effect"
import { PluginV2 } from "../../plugin"

export const VercelPlugin = PluginV2.define({
  id: PluginV2.ID.make("vercel"),
  effect: Effect.gen(function* () {
    return {
      "catalog.transform": Effect.fn(function* (evt) {
        for (const item of evt.data) {
          if (item.provider.endpoint.type !== "aisdk") continue
          if (item.provider.endpoint.package !== "@ai-sdk/vercel") continue
          evt.provider.update(item.provider.id, (provider) => {
            // == ALKERA EDIT START — outbound attribution headers name the product (provider-facing)
            provider.options.headers["http-referer"] = "https://alkera.ai/"
            provider.options.headers["x-title"] = "alkera"
            // == ALKERA EDIT END
          })
        }
      }),
      "aisdk.sdk": Effect.fn(function* (evt) {
        if (evt.package !== "@ai-sdk/vercel") return
        const mod = yield* Effect.promise(() => import("@ai-sdk/vercel"))
        evt.sdk = mod.createVercel(evt.options)
      }),
    }
  }),
})
