import type { ScenarioIntent, ScenarioPersona } from "@/content/en/scenarios";

const DEMO_ORIGIN = "https://demo.crew.day";
const DEMO_SANDBOX =
  "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-top-navigation-by-user-activation";

type DemoFrameCopy = {
  liveFrameTitle: string;
  liveModeLabel: string;
  liveModeNotice: string;
  tryLiveLabel: string;
  tryLiveNotice: string;
  videoFallback: string;
  videoModeLabel: string;
};

export type DemoSelection = {
  persona: ScenarioPersona;
  intent: ScenarioIntent;
};

export function buildDemoUrl(selection: DemoSelection): string {
  const { intent, persona } = selection;
  return `${DEMO_ORIGIN}/app?scenario=${persona.scenarioKey}&as=${persona.as}&start=${encodeURIComponent(intent.start)}`;
}

export function DemoFrame({
  copy,
  liveMode,
  onTryLive,
  prefersReducedMotion,
  selection,
}: {
  copy: DemoFrameCopy;
  liveMode: boolean;
  onTryLive: () => void;
  prefersReducedMotion: boolean;
  selection: DemoSelection;
}) {
  const videoBase = `/demo/${selection.persona.scenarioKey}/${selection.intent.slug}`;
  const demoUrl = buildDemoUrl(selection);

  return (
    <div className="demo-frame" data-mode={liveMode ? "live" : "video"}>
      <div className="demo-frame__chrome" aria-hidden="true">
        <span></span>
        <span></span>
        <span></span>
      </div>
      <div className="demo-frame__viewport">
        {liveMode ? (
          <iframe
            className="demo-frame__embed"
            src={demoUrl}
            title={copy.liveFrameTitle}
            sandbox={DEMO_SANDBOX}
            loading="lazy"
            referrerPolicy="strict-origin-when-cross-origin"
          />
        ) : (
          <video
            className="demo-frame__video"
            aria-label={selection.intent.caption}
            autoPlay={!prefersReducedMotion}
            controls={prefersReducedMotion}
            loop
            muted
            playsInline
            preload="metadata"
          >
            <source src={`${videoBase}.webm`} type="video/webm" />
            <source src={`${videoBase}.mp4`} type="video/mp4" />
            {copy.videoFallback}
          </video>
        )}
        <div className="demo-frame__caption">
          <p className="demo-frame__mode">
            {liveMode ? copy.liveModeLabel : copy.videoModeLabel}
          </p>
          <p>{selection.intent.caption}</p>
        </div>
      </div>
      <div className="demo-frame__action" aria-live="polite">
        {liveMode ? (
          <p className="demo-frame__status">{copy.liveModeLabel}</p>
        ) : (
          <button className="button button--primary demo-frame__button" type="button" onClick={onTryLive}>
            {copy.tryLiveLabel}
          </button>
        )}
        <p>{liveMode ? copy.liveModeNotice : copy.tryLiveNotice}</p>
      </div>
    </div>
  );
}
