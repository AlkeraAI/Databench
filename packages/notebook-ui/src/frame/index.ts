// The content-origin output frame: the host, the renderers that use it, and
// the wire contract both sides share.

export { FrameHost, modelReferences } from "./host";
export type { DropReason, FrameHostEvents, FrameHostOptions, FrameWindow, InboundMessage } from "./host";
export { FRAME_LOAD_TIMEOUT_MS, FRAME_MIMES, FramedOutput, frameRenderers } from "./FramedOutput";
export { WIDGETS_BUNDLE_REGISTRATION, WIDGETS_MODULE, WIDGETS_PRELUDE, withWidgetsAdapter } from "./widgetsAdapter";
export { isPlatformFrameModule, loadPlatformFrameModule, platformModuleLoader } from "./platformModules";
export {
  COMM_SENDS_PER_SECOND,
  FRAME_MODULE_BY_MIME,
  FRAME_PROTOCOL,
  MAX_COMM_SEND_BYTES,
  MAX_FRAME_HEIGHT,
  PLATFORM_FRAME_MODULES,
  WIDGET_VIEW_MIME,
} from "./protocol";
export type {
  FrameApi,
  FrameInit,
  FrameMessage,
  FrameMessageType,
  FrameModule,
  FrameModuleRef,
  ParentMessage,
  PlatformFrameModule,
} from "./protocol";
