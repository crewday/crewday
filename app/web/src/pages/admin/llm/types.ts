import type { LlmAssignment } from "@/types";

export type Column = "provider" | "model" | "assignment" | "capability";

export interface Selection {
  column: Column;
  id: string;
}

export interface Highlighted {
  providers: Set<string>;
  models: Set<string>;
  providerModels: Set<string>;
  assignments: Set<string>;
  capabilities: Set<string>;
}

export interface EdgeLayout {
  id: string;
  kind: "pm" | "assign";
  providerId: string;
  modelId: string;
  providerModelId: string;
  assignmentId?: string;
  capability?: string;
  d: string;
  invalid: boolean;
}

export type NodeClass = (col: Column, id: string) => string;
export type ElementRefSetter = (id: string) => (el: HTMLElement | null) => void;
export type SelectionSetter = (selection: Selection | null) => void;

export interface AssignmentMutationTarget {
  assignment: LlmAssignment;
  providerModelId: string;
}
