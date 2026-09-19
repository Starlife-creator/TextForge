let abortController: AbortController | null = null;
let lastEventTime = Date.now();

export interface SSEHandlers {
  onEvent: (event: string, payload: any) => void;
  onError: (msg: string) => void;
}

export async function connectSSE(port: number, handlers: SSEHandlers, retries = 0) {
  abortController = new AbortController();
  try {
    const response = await fetch(`http://127.0.0.1:${port}/api/events`, {
      signal: abortController.signal,
      headers: { 'Accept': 'text/event-stream' },
    });
    const reader = response.body!.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    lastEventTime = Date.now();
    const timeoutCheck = setInterval(() => {
      if (Date.now() - lastEventTime > 60000) {
        abortController?.abort();
        clearInterval(timeoutCheck);
      }
    }, 10000);
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      lastEventTime = Date.now();
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const event = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        parseSSEEvent(event, handlers);
      }
    }
    clearInterval(timeoutCheck);
  } catch (e) {
    if (retries < 5) {
      setTimeout(() => connectSSE(port, handlers, retries + 1), Math.pow(2, retries) * 1000);
    } else {
      handlers.onError('后端连接断开，请点击"重启后端"');
    }
  }
}

export function disconnectSSE() {
  abortController?.abort();
}

function parseSSEEvent(raw: string, handlers: SSEHandlers) {
  const lines = raw.split('\n');
  let eventType = 'message';
  const dataLines: string[] = [];
  for (const line of lines) {
    if (line.startsWith('event:')) eventType = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
  }
  if (dataLines.length === 0) return;
  try { handlers.onEvent(eventType, JSON.parse(dataLines.join('\n'))); }
  catch (e) { console.error('SSE 解析失败:', dataLines); }
}
