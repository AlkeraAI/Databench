import { Effect } from "effect"
import { PluginV2 } from "../../plugin"

export const NvidiaPlugin = PluginV2.define({
  id: PluginV2.ID.make("nvidia"),
  effect: Effect.gen(function* () {
    return {
      "catalog.transform": Effect.fn(function* (evt) {
        for (const item of evt.data) {
          if (item.provider.endpoint.type !== "aisdk") continue
          if (item.provider.endpoint.package !== "@ai-sdk/openai-compatible") continue
          if (item.provider.endpoint.url !== "https://integrate.api.nvidia.com/v1") continue
          evt.provider.update(item.provider.id, (provider) => {
            // == ALKERA EDIT START — outbound attribution headers name the product (provider-facing)
            provider.options.headers["HTTP-Referer"] = "https://alkera.ai/"
            provider.options.headers["X-Title"] = "alkera"
            provider.options.headers["X-BILLING-INVOKE-ORIGIN"] ??= "Alkera"
            // == ALKERA EDIT END
          })
        }
      }),
    }
  }),
})
