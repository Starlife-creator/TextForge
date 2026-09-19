from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import asyncio, json
from routes import state

router = APIRouter()
_subscribers: dict[str, list[asyncio.Queue]] = {}

def sse_emit(event: str, payload: dict, run_id: str | None = None):
    rid = run_id or state.current_run_id
    if not rid: return
    for q in list(_subscribers.get(rid, [])):
        try:
            q.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            # done/error 时丢旧事件
            if event in ("done", "error"):
                try:
                    q.get_nowait()
                    q.put_nowait({"event": event, "data": payload})
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

@router.get("/api/events")
async def events(run_id: str | None = None):
    q = asyncio.Queue(maxsize=1000)
    async def gen():
        nonlocal run_id
        if not run_id:
            for _ in range(120):
                if state.current_run_id:
                    run_id = state.current_run_id
                    break
                await asyncio.sleep(0.5)
            if not run_id:
                yield f"event: error\ndata: {json.dumps({'kind':'unknown','code':'no_run','message':'等待 run_id 超时'})}\n\n"
                return
        _subscribers.setdefault(run_id, []).append(q)
        yield f"event: run_id\ndata: {json.dumps({'run_id': run_id})}\n\n"
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            return
        finally:
            if run_id in _subscribers:
                try: _subscribers[run_id].remove(q)
                except ValueError: pass
                if not _subscribers[run_id]: del _subscribers[run_id]
    return StreamingResponse(gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
