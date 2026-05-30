import { buildDemoUrl, type DemoSelection } from "./DemoFrame.lib";

const DEMO_SANDBOX =
  "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-top-navigation-by-user-activation";

type DemoFrameCopy = {
  liveFrameTitle: string;
  liveModeLabel: string;
  liveModeNotice: string;
  previewEmphasis: string;
  previewLabel: string;
  tryLiveLabel: string;
  tryLiveNotice: string;
  videoFallback: string;
  videoModeLabel: string;
};

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
  const displayUrl = `demo.crew.day/app?scenario=${selection.persona.scenarioKey}&as=${selection.persona.as}&start=${encodeURIComponent(selection.intent.start)}`;

  return (
    <div className="demo-frame" data-mode={liveMode ? "live" : "video"}>
      <p className="demo-frame__label">
        {copy.previewLabel} · <em>{copy.previewEmphasis}</em>
      </p>
      <div className="demo-frame__viewport">
        <div className="demo-frame__chrome" aria-hidden="true">
          <span></span>
          <span></span>
          <span></span>
          <strong>{displayUrl}</strong>
        </div>
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
        <code className="demo-frame__url">{displayUrl}</code>
        {liveMode ? (
          <p className="demo-frame__status">{copy.liveModeLabel}</p>
        ) : (
          <button className="demo-frame__button" type="button" onClick={onTryLive}>
            {copy.tryLiveLabel}
          </button>
        )}
        <p>{liveMode ? copy.liveModeNotice : copy.tryLiveNotice}</p>
      </div>
    </div>
  );
}
