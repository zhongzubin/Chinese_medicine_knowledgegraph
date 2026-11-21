import { describe, expect, it, vi } from "vitest";

import { consumeSseStream, parseSseBlock } from "@/services/sse";

describe("SSE parser", () => {
  it("parses event, multiline data and comments", () => {
    const event = parseSseBlock(
      ": keep-alive\nevent: warning\ndata: 第一行\ndata: 第二行\nid: 7",
    );

    expect(event).toEqual({
      event: "warning",
      data: "第一行\n第二行",
      id: "7",
    });
  });

  it("consumes events split across arbitrary byte chunks", async () => {
    const chunks = [
      "event: stage\r\ndata: {\"stage\":\"entity\"",
      "}\r\n\r\nevent: done\ndata: {\"ok\":true}\n\n",
    ];
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    });
    const onEvent = vi.fn();

    await consumeSseStream(stream, onEvent);

    expect(onEvent).toHaveBeenCalledTimes(2);
    expect(onEvent).toHaveBeenNthCalledWith(1, {
      event: "stage",
      data: '{"stage":"entity"}',
    });
    expect(onEvent).toHaveBeenNthCalledWith(2, {
      event: "done",
      data: '{"ok":true}',
    });
  });
});
