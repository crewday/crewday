import type { ScenarioIntent, ScenarioPersona } from "@/content/en/scenarios";

const DEMO_ORIGIN = "https://demo.crew.day";

export type DemoSelection = {
  persona: ScenarioPersona;
  intent: ScenarioIntent;
};

export function buildDemoUrl(selection: DemoSelection): string {
  const { intent, persona } = selection;
  return `${DEMO_ORIGIN}/app?scenario=${persona.scenarioKey}&as=${persona.as}&start=${encodeURIComponent(intent.start)}`;
}
