import { useEffect, useState } from "react";
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

function writeHash(persona: ScenarioPersona, intent: ScenarioIntent): void {
  const nextHash = `#try-it?persona=${persona.key}&intent=${intent.slug}`;
  const nextUrl = `${window.location.pathname}${window.location.search}${nextHash}`;
  window.history.replaceState(null, "", nextUrl);
}

function useReducedMotion(): boolean {
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(true);

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    setPrefersReducedMotion(query.matches);

    const handleChange = () => setPrefersReducedMotion(query.matches);
    query.addEventListener("change", handleChange);
    return () => query.removeEventListener("change", handleChange);
  }, []);

  return prefersReducedMotion;
}

export function ScenarioPicker({
  fixedPersona,
}: {
  fixedPersona?: PersonaKey;
}) {
  const initialSelection = {
    personaKey: fixedPersona ?? DEFAULT_PERSONA,
    intentSlug: undefined,
  };
  const [personaKey, setPersonaKey] = useState<PersonaKey>(initialSelection.personaKey);
  const [intentSlug, setIntentSlug] = useState<IntentSlug | undefined>(
    initialSelection.intentSlug,
  );
  const [liveMode, setLiveMode] = useState(false);
  const prefersReducedMotion = useReducedMotion();

  const persona = findPersona(personaKey);
  const intent = findIntent(persona, intentSlug);
  const shouldShowPersonaAxis = fixedPersona === undefined;

  useEffect(() => {
    const hashSelection = readHashSelection(fixedPersona);
    const hashPersona = findPersona(hashSelection.personaKey);
    const hashIntent = findIntent(hashPersona, hashSelection.intentSlug);
    setPersonaKey(hashSelection.personaKey);
    setIntentSlug(hashSelection.intentSlug);

    if (window.location.hash.startsWith("#try-it?")) {
      writeHash(hashPersona, hashIntent);
      document.getElementById("try-it")?.scrollIntoView({ block: "start" });
    }
  }, [fixedPersona]);

  useEffect(() => {
    if (fixedPersona !== undefined && fixedPersona !== personaKey) {
      const nextPersona = findPersona(fixedPersona);
      setPersonaKey(fixedPersona);
      setIntentSlug(nextPersona.intents[0].slug);
    }
  }, [fixedPersona, personaKey]);

  function selectPersona(nextPersonaKey: PersonaKey): void {
    const nextPersona = findPersona(nextPersonaKey);
    const nextIntent = nextPersona.intents[0];
    setPersonaKey(nextPersona.key);
    setIntentSlug(nextIntent.slug);
    writeHash(nextPersona, nextIntent);
  }

  function selectIntent(nextIntent: ScenarioIntent): void {
    setIntentSlug(nextIntent.slug);
    writeHash(persona, nextIntent);
  }

  return (
    <div className="scenario-picker">
      <div className="scenario-picker__intro">
        <p className="eyebrow">{scenarioPickerCopy.eyebrow}</p>
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
                    <span>{option.shortLabel}</span>
                    <small>{option.label}</small>
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
              {persona.intents.map((option) => (
                <button
                  aria-pressed={option.slug === intent.slug}
                  className="scenario-picker__option"
                  key={option.slug}
                  type="button"
                  onClick={() => selectIntent(option)}
                >
                  {option.label}
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
