import { useEffect, useState, useSyncExternalStore } from "react";
import {
  scenarioPersonas,
  scenarioPickerCopy,
  type IntentSlug,
  type PersonaKey,
  type ScenarioIntent,
  type ScenarioPersona,
} from "@/content/en/scenarios";
import { DemoFrame } from "./DemoFrame";

const DEFAULT_PERSONA = "villa-owner" satisfies PersonaKey;

function findPersona(personaKey: PersonaKey): ScenarioPersona {
  return (
    scenarioPersonas.find((persona) => persona.key === personaKey) ??
    scenarioPersonas[0]
  );
}

function findIntent(persona: ScenarioPersona, intentSlug?: string): ScenarioIntent {
  return (
    persona.intents.find((intent) => intent.slug === intentSlug) ??
    persona.intents[0]
  );
}

function isPersonaKey(value: string | null): value is PersonaKey {
  return scenarioPersonas.some((persona) => persona.key === value);
}

function readHashSelection(fixedPersona?: PersonaKey): {
  personaKey: PersonaKey;
  intentSlug?: IntentSlug;
} {
  if (typeof window === "undefined") {
    return { personaKey: fixedPersona ?? DEFAULT_PERSONA };
  }

  const hash = window.location.hash;
  const queryStart = hash.startsWith("#try-it?") ? hash.slice("#try-it?".length) : "";
  const params = new URLSearchParams(queryStart);
  const hashPersona = params.get("persona");
  const personaKey = fixedPersona ?? (isPersonaKey(hashPersona) ? hashPersona : DEFAULT_PERSONA);
  const persona = findPersona(personaKey);
  const hashIntent = params.get("intent");
  const intentSlug = persona.intents.some((intent) => intent.slug === hashIntent)
    ? (hashIntent as IntentSlug)
    : undefined;

  return { personaKey, intentSlug };
}

function normalizeSelection(
  selection: { personaKey: PersonaKey; intentSlug?: IntentSlug },
  fixedPersona?: PersonaKey,
): {
  persona: ScenarioPersona;
  intent: ScenarioIntent;
} {
  const persona = findPersona(fixedPersona ?? selection.personaKey);
  const intent = findIntent(persona, selection.intentSlug);
  return { persona, intent };
}

function writeHash(persona: ScenarioPersona, intent: ScenarioIntent): void {
  const nextHash = `#try-it?persona=${persona.key}&intent=${intent.slug}`;
  const nextUrl = `${window.location.pathname}${window.location.search}${nextHash}`;
  window.history.replaceState(null, "", nextUrl);
}

function subscribeToReducedMotion(onStoreChange: () => void): () => void {
  const query = window.matchMedia("(prefers-reduced-motion: reduce)");
  query.addEventListener("change", onStoreChange);
  return () => query.removeEventListener("change", onStoreChange);
}

function readReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribeToReducedMotion, readReducedMotion, () => true);
}

export function ScenarioPicker({
  fixedPersona,
}: {
  fixedPersona?: PersonaKey;
}) {
  const [selection, setSelection] = useState(() => readHashSelection(fixedPersona));
  const [liveMode, setLiveMode] = useState(false);
  const prefersReducedMotion = useReducedMotion();

  const { intent, persona } = normalizeSelection(selection, fixedPersona);
  const shouldShowPersonaAxis = fixedPersona === undefined;

  useEffect(() => {
    const syncFromHash = () => {
      if (!window.location.hash.startsWith("#try-it?")) {
        return;
      }

      const hashSelection = readHashSelection(fixedPersona);
      const hashPersona = findPersona(hashSelection.personaKey);
      const hashIntent = findIntent(hashPersona, hashSelection.intentSlug);
      setSelection({ personaKey: hashSelection.personaKey, intentSlug: hashIntent.slug });
      writeHash(hashPersona, hashIntent);
      document.getElementById("try-it")?.scrollIntoView({ block: "start" });
    };

    syncFromHash();
    window.addEventListener("hashchange", syncFromHash);
    return () => window.removeEventListener("hashchange", syncFromHash);
  }, [fixedPersona]);

  function selectPersona(nextPersonaKey: PersonaKey): void {
    const nextPersona = findPersona(nextPersonaKey);
    const nextIntent = nextPersona.intents[0];
    setSelection({ personaKey: nextPersona.key, intentSlug: nextIntent.slug });
    writeHash(nextPersona, nextIntent);
  }

  function selectIntent(nextIntent: ScenarioIntent): void {
    setSelection({ personaKey: persona.key, intentSlug: nextIntent.slug });
    writeHash(persona, nextIntent);
  }

  return (
    <div className="scenario-picker">
      <div className="scenario-picker__heading">
        <h2>{scenarioPickerCopy.heading}</h2>
        <p>{scenarioPickerCopy.body}</p>
      </div>

      <div className="scenario-picker__layout">
        <div className="scenario-picker__controls">
          {shouldShowPersonaAxis ? (
            <fieldset className="scenario-picker__group">
              <legend>{scenarioPickerCopy.personaLegend}</legend>
              <div className="scenario-picker__options scenario-picker__options--persona">
                {scenarioPersonas.map((option) => (
                  <button
                    aria-pressed={option.key === persona.key}
                    className="scenario-picker__option"
                    key={option.key}
                    type="button"
                    onClick={() => selectPersona(option.key)}
                  >
                    <span className="scenario-picker__glyph">{option.shortLabel[0]}</span>
                    <span>{option.shortLabel}</span>
                    <small>
                      {scenarioPickerCopy.scenarioMetaLabel} · {option.scenarioKey}
                    </small>
                  </button>
                ))}
              </div>
            </fieldset>
          ) : (
            <div className="scenario-picker__locked-persona">
              <p>{scenarioPickerCopy.lockedPersonaLabel}</p>
              <strong>{persona.label}</strong>
            </div>
          )}

          <fieldset className="scenario-picker__group">
            <legend>{scenarioPickerCopy.intentLegend}</legend>
            <div className="scenario-picker__options scenario-picker__options--intent">
              {persona.intents.map((option, index) => (
                <button
                  aria-pressed={option.slug === intent.slug}
                  className="scenario-picker__option"
                  key={option.slug}
                  type="button"
                  onClick={() => selectIntent(option)}
                >
                  <span className="scenario-picker__glyph">{index + 1}</span>
                  <span>{option.label}</span>
                  <small>
                    {scenarioPickerCopy.startMetaLabel} · {option.start}
                  </small>
                </button>
              ))}
            </div>
          </fieldset>
        </div>

        <DemoFrame
          copy={scenarioPickerCopy}
          liveMode={liveMode}
          onTryLive={() => setLiveMode(true)}
          prefersReducedMotion={prefersReducedMotion}
          selection={{ persona, intent }}
        />
      </div>
    </div>
  );
}
