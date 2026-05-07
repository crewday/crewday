import { scenarioPersonas } from "../src/content/en/scenarios";

type RecordingTarget = {
  scenario: string;
  as: string;
  intent: string;
  start: string;
  webm: string;
  mp4: string;
};

export const recordingTargets: readonly RecordingTarget[] = scenarioPersonas.flatMap(
  (persona) =>
    persona.intents.map((intent) => ({
      scenario: persona.scenarioKey,
      as: persona.as,
      intent: intent.slug,
      start: intent.start,
      webm: `public/demo/${persona.scenarioKey}/${intent.slug}.webm`,
      mp4: `public/demo/${persona.scenarioKey}/${intent.slug}.mp4`,
    })),
);

export const deterministicRecordingWorkflow = [
  "Bring up the demo deployment locally with CREWDAY_DEMO_MODE=1 and a fixed public URL for Playwright.",
  "Freeze the browser clock and RNG before each run; use the scenario fixture's relative start path as the first navigation target.",
  "For each recording target, visit /app with scenario, as, and start query params, wait for the workspace redirect, then drive the scripted intent path.",
  "Record a silent 30-45 second viewport capture, encode VP9 WebM and H.264 MP4, and write both files to the target paths below.",
  "Re-run the site smoke after recording to confirm the landing page requests only same-origin demo media before the live iframe button is clicked.",
] as const;

export function recordingManifest(): readonly RecordingTarget[] {
  return recordingTargets;
}
