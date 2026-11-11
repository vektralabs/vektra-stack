// The UI's explicit state machine (plan 9): one pure reducer, no side effects. The server
// stays authoritative: every state below follows a server answer or a server event.

export type CloseReason =
  | "finished"
  | "timed_out"
  | "service_failure"
  | "budget_exhausted"
  | "terminated_by_teacher"
  | "access_revoked";

export type UiState =
  | { name: "awaiting_handoff" }
  | { name: "resuming" }
  | { name: "preparing"; announced: boolean }
  | { name: "active" }
  | { name: "outage" }
  | { name: "closed"; reason: CloseReason | "unknown" }
  | { name: "capacity_refused" }
  | { name: "start_rolled_back"; reason: string }
  | { name: "unavailable" };

export type Action =
  | { type: "handoff"; mode: "start" | "resume" }
  | { type: "start_accepted" } // Start answered 202
  | { type: "capacity_refused" } // Start answered 503 capacity
  | { type: "unavailable" } // mint failed, a refused Start, a lost token, a network failure
  | { type: "session_state"; state: string; closeReason?: string | null } // GET or SSE state
  | { type: "committed" }
  | { type: "rolled_back"; reason: string }
  | { type: "outage_start" }
  | { type: "outage_end" }
  | { type: "closed"; reason?: string | null };

const REASONS: ReadonlySet<string> = new Set([
  "finished",
  "timed_out",
  "service_failure",
  "budget_exhausted",
  "terminated_by_teacher",
  "access_revoked",
]);

export const INITIAL: UiState = { name: "awaiting_handoff" };

function closed(reason: string | null | undefined): UiState {
  return { name: "closed", reason: reason && REASONS.has(reason) ? (reason as CloseReason) : "unknown" };
}

export function isTerminal(s: UiState): boolean {
  return ["closed", "capacity_refused", "start_rolled_back", "unavailable"].includes(s.name);
}

export function reduce(s: UiState, a: Action): UiState {
  if (s.name === "closed") {
    // a closed session never reopens; a close learnt without its reason (a 409) takes the
    // reason of the closed event that follows, once
    return a.type === "closed" && s.reason === "unknown" && a.reason && REASONS.has(a.reason)
      ? closed(a.reason)
      : s;
  }
  switch (a.type) {
    case "handoff":
      return s.name === "awaiting_handoff"
        ? a.mode === "start"
          ? { name: "preparing", announced: false }
          : { name: "resuming" }
        : s;
    case "start_accepted":
      return s.name === "preparing" ? { name: "preparing", announced: true } : s;
    case "capacity_refused":
      return s.name === "preparing" ? { name: "capacity_refused" } : s;
    case "unavailable":
      return isTerminal(s) ? s : { name: "unavailable" };
    case "committed":
      return s.name === "preparing" || s.name === "resuming" ? { name: "active" } : s;
    case "rolled_back":
      return s.name === "preparing" || s.name === "resuming"
        ? { name: "start_rolled_back", reason: a.reason }
        : s;
    case "outage_start":
      return s.name === "active" ? { name: "outage" } : s;
    case "outage_end":
      return s.name === "outage" ? { name: "active" } : s;
    case "closed":
      return closed(a.reason);
    case "session_state":
      switch (a.state) {
        case "STARTING":
          return s.name === "resuming" ? { name: "preparing", announced: true } : s;
        case "ACTIVE":
          return s.name === "preparing" || s.name === "resuming" || s.name === "outage"
            ? { name: "active" }
            : s;
        case "OUTAGE":
          return s.name === "active" || s.name === "resuming" || s.name === "preparing"
            ? { name: "outage" }
            : s;
        case "CLOSED":
          return closed(a.closeReason);
        default:
          return s;
      }
  }
}
