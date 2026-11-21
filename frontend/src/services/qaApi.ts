import type { ApiEnvelope, GraphQAResponse, QAStreamEvent } from "@/types/api";
import { consumeSseStream } from "@/services/sse";

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
  }
}

async function readError(response: Response): Promise<ApiRequestError> {
  try {
    const envelope = (await response.json()) as ApiEnvelope<unknown>;
    return new ApiRequestError(
      response.status,
      envelope.error?.code || "REQUEST_FAILED",
      envelope.error?.message || "请求失败，请稍后重试",
    );
  } catch {
    return new ApiRequestError(
      response.status,
      "REQUEST_FAILED",
      "请求失败，请稍后重试",
    );
  }
}

export async function streamQuestion(
  question: string,
  onEvent: (event: QAStreamEvent) => void | Promise<void>,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch("/api/v2/qa/stream", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ question }),
    signal,
  });

  if (!response.ok) {
    throw await readError(response);
  }
  if (!response.body) {
    throw new ApiRequestError(
      502,
      "EMPTY_STREAM",
      "服务端未返回流式响应",
    );
  }

  await consumeSseStream(response.body, async (event) => {
    if (!event.data) {
      return;
    }
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(event.data) as Record<string, unknown>;
    } catch {
      throw new ApiRequestError(
        502,
        "INVALID_STREAM",
        "服务端返回了无法解析的事件",
      );
    }
    await onEvent({ event: event.event, data });
  });
}

export async function answerQuestion(
  question: string,
): Promise<GraphQAResponse> {
  const response = await fetch("/api/v2/qa", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({ question }),
  });
  if (!response.ok) {
    throw await readError(response);
  }
  const envelope = (await response.json()) as ApiEnvelope<GraphQAResponse>;
  if (envelope.error || !envelope.data) {
    throw new ApiRequestError(
      response.status,
      envelope.error?.code || "EMPTY_RESPONSE",
      envelope.error?.message || "服务端未返回回答",
    );
  }
  return envelope.data;
}

export async function clearAnonymousSession(): Promise<void> {
  const response = await fetch("/api/v2/session", {
    method: "DELETE",
    credentials: "include",
    headers: {
      Accept: "application/json",
    },
  });
  if (!response.ok) {
    throw await readError(response);
  }
}
