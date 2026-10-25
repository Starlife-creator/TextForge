/**
 * #11 SSE 事件 composable（骨架，尚未接入 App.vue）。
 *
 * 迁移口径：`src/App.vue` 的 `sseHandlers` / `connectSSE` / `disconnectSSE` 仍是现行
 * 实现；本文件把「连接事件流、分发事件、暴露连接状态」抽成可测单元，待 npm 环境下
 * 分批替换。未接入前不要在组件里调用本文件。
 *
 * 本轮缺 npm 构建/组件测试环境，未执行 `npm run build` / `vitest`（#11 ⬜）。
 */

import { ref, onUnmounted, type Ref } from 'vue';
import { connectSSE, disconnectSSE, type SSEHandlers } from '../api/sse';

export function useSseEvents(port: Ref<number | null>) {
  const connected = ref(false);
  const lastEvent = ref<{ event: string; payload: unknown } | null>(null);

  function start(handlers: Partial<SSEHandlers> = {}): void {
    if (!port.value) return;
    connectSSE(port.value, {
      onEvent: (event, payload) => {
        lastEvent.value = { event, payload };
        handlers.onEvent?.(event, payload);
      },
      onStatus: (s) => {
        connected.value = s.connected;
        handlers.onStatus?.(s);
      },
    });
  }

  function stop(): void {
    disconnectSSE();
    connected.value = false;
  }

  onUnmounted(stop);

  return { connected, lastEvent, start, stop };
}
