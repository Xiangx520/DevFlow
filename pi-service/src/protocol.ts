import {StringDecoder} from "node:string_decoder";
export interface Command {
  v: 1;
  id: string;
  type: "start" | "resume" | "steer" | "cancel" | "status";
  task_id?: string;
  attempt_id?: string;
  session_id?: string;
  capability?: string;
  api_url?: string;
  model?: string;
  mandate?: Record<string, unknown>;
  text?: string;
}
export function parseCommand(line: string): Command {
  const value: unknown = JSON.parse(line);
  if (!value || typeof value !== "object") throw new Error("command must be an object");
  const item = value as Record<string, unknown>;
  if (item.v !== 1 || typeof item.id !== "string" || !["start","resume","steer","cancel","status"].includes(String(item.type)))
    throw new Error("invalid protocol envelope");
  if (item.type === "start" || item.type === "resume") {
    for (const key of ["task_id","attempt_id","session_id","capability","api_url","model"])
      if (typeof item[key] !== "string" || !item[key]) throw new Error("missing " + key);
    if (!/^[a-f0-9-]{36}$/.test(String(item.session_id))) throw new Error("invalid session identifier");
    if (!item.mandate || typeof item.mandate !== "object") throw new Error("missing mandate");
  }
  return item as unknown as Command;
}
export async function* records(input: AsyncIterable<Buffer | string>): AsyncGenerator<string> {
  let buffer = "";
  const decoder = new StringDecoder("utf8");
  for await (const chunk of input) {
    buffer += typeof chunk === "string" ? chunk : decoder.write(chunk);
    if (Buffer.byteLength(buffer) > 2 * 1024 * 1024) throw new Error("protocol record too large");
    let at: number;
    while ((at = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, at).replace(/\r$/, "");
      buffer = buffer.slice(at + 1);
      if (line) yield line;
    }
  }
  if (buffer.trim()) throw new Error("incomplete protocol record");
}
