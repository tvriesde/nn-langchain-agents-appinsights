export type Recipe = {
  title: string; servings: number; ingredients: string[]; steps: string[]; notes: string;
};
export type Pairing = { wine: string; rationale: string; serving_tip: string };
export type Menu = {
  title: string; introduction: string; recipe: Recipe; pairing: Pairing; review_notes: string;
};
export type Turn = {
  id: string; message: string; status: "pending" | "completed" | "failed";
  trace_id: string; menu: Menu | null; error: string | null; context_truncated: boolean;
};
export type StreamEvent = { event: string; data: Record<string, unknown> };

export function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("The server returned an invalid response.");
  }
  return value as Record<string, unknown>;
}

export function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("The server returned an invalid text field.");
  return value;
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) throw new Error("The server returned an invalid list.");
  return value.map(text);
}

export function parseTurn(value: unknown): Turn {
  const turn = record(value);
  if (!["pending", "completed", "failed"].includes(text(turn.status))) {
    throw new Error("The server returned an invalid turn status.");
  }
  let menu: Menu | null = null;
  if (turn.menu) {
    const m = record(turn.menu);
    const recipe = record(m.recipe);
    const pairing = record(m.pairing);
    if (typeof recipe.servings !== "number") throw new Error("Invalid serving count.");
    menu = {
      title: text(m.title), introduction: text(m.introduction), review_notes: text(m.review_notes),
      recipe: {
        title: text(recipe.title), servings: recipe.servings,
        ingredients: strings(recipe.ingredients), steps: strings(recipe.steps), notes: text(recipe.notes),
      },
      pairing: {
        wine: text(pairing.wine), rationale: text(pairing.rationale), serving_tip: text(pairing.serving_tip),
      },
    };
  }
  return {
    id: text(turn.id), message: text(turn.message),
    status: turn.status === "completed" ? "completed" : turn.status === "failed" ? "failed" : "pending",
    trace_id: text(turn.trace_id), menu, error: turn.error ? text(turn.error) : null,
    context_truncated: turn.context_truncated === true,
  };
}

export async function request(url: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(url, { credentials: "same-origin", ...init });
  if (!response.ok) {
    const content: unknown = await response.json();
    const detail = record(content).detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${response.status}).`);
  }
  return response;
}

export async function* events(response: Response): AsyncGenerator<StreamEvent> {
  if (!response.body) throw new Error("Streaming is unavailable in this browser.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let end: number;
      while ((end = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const lines = block.split("\n");
        const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
        const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5)).join("\n");
        if (event && data) yield { event, data: record(JSON.parse(data)) };
      }
    }
    if (buffer.trim()) throw new Error("The response stream ended unexpectedly. Refresh history.");
  } finally {
    reader.releaseLock();
  }
}
