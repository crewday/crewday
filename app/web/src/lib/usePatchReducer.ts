import { useReducer, type Dispatch } from "react";

export type PatchReducerAction<TState> =
  | Partial<TState>
  | ((state: TState) => TState);

function patchReducer<TState>(
  state: TState,
  action: PatchReducerAction<TState>,
): TState {
  if (typeof action === "function") return action(state);
  return { ...state, ...action };
}

export function usePatchReducer<TState>(
  initialState: TState | (() => TState),
): [TState, Dispatch<PatchReducerAction<TState>>] {
  return useReducer(
    patchReducer<TState>,
    initialState,
    (value) => (typeof value === "function" ? (value as () => TState)() : value),
  );
}
