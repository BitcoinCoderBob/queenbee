import asyncio
import contextlib
import json
import logging
import os
import random
import re
import time
from asyncio import Queue
from threading import RLock
from typing import Iterator, Optional, Generator

import fastapi
import httpx
import starlette.websockets
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, Request, HTTPException

from .openai_types import CompletionChunk, ChatCompletion, CreateChatCompletionRequest

from fastapi.middleware.cors import CORSMiddleware

from starlette.middleware.sessions import SessionMiddleware
from sse_starlette.sse import EventSourceResponse

log = logging.getLogger(__name__)

load_dotenv()

SECRET_KEY = os.environ["SECRET_KEY"]
BILLING_URL = os.environ["BILLING_URL"]

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


def check_creds_and_funds(request):
    # todo: this just needs to check if balance > 0
    return True


def bill_usage(request, msize: int, usage: dict, worker_info: dict, secs: int):
    # todo: this should bill based on model size * usage
    pay_to_lnurl = worker_info.get("ln_url")
    pay_to_auth = worker_info.get("auth_key")

    req_user = request.headers.get("Authorization")
    bill_to_token = ""
    if req_user and " " in req_user:
        bill_to_token = req_user.split(" ")[1]

    command = dict(
        command="complete",
        bill_to_token=bill_to_token,
        pay_to_lnurl=pay_to_lnurl,
        pay_to_auth=pay_to_auth,
    )

    res = httpx.post(BILLING_URL, json=command)

    if res.status_code != 200:
        log.error("bill endpoint: %s/%s", res.status_code, res.text)
        log.error("bill %s/%s/%s to: (%s), pay to: (%s)", usage, msize, secs, req_user, worker_info)

    return True


def check_bill_usage(request, msize: int, js: dict, worker_info: dict, secs: int):
    if js.get("usage"):
        bill_usage(request, msize, js["usage"], worker_info, secs)

@app.post("/v1/chat/completions")
async def create_chat_completion(
        request: Request,
        body: CreateChatCompletionRequest,
) -> ChatCompletion:
    check_creds_and_funds(request)

    web_only = body.model.startswith("webgpu/")
    msize = get_model_size(body.model)
    mgr = get_reg_mgr()
    gpu_filter = body.gpu_filter

    try:
        try:
            with mgr.get_socket_for_inference(msize, web_only, gpu_filter) as ws:
                return await do_inference(request, body, ws)
        except fastapi.WebSocketDisconnect:
            with mgr.get_socket_for_inference(msize, web_only, gpu_filter) as ws:
                return await do_inference(request, body, ws)
    except Exception as ex:
        raise HTTPException(500, detail=repr(ex))


def augment_reply(body: CreateChatCompletionRequest, js):
    # todo: this should be used to VALIDATE the reply, not "fix" it!

    inp = sum(len(msg.content) for msg in body.messages)//3
    out = len(json.dumps(js["choices"][0]["message"]["content"]))//3

    # todo: this all happens because the web-worker is total hack, need to clean it up
    if not js.get("model"):
        js["model"] = body.model

    if not js.get("object"):
        js["object"] = "chat.completion"

    if not js.get("created"):
        js["created"] = int(time.time())

    if not js.get("id"):
        js["id"] = os.urandom(16).hex()

    if not js.get("usage"):
        js["usage"] = dict(
            prompt_tokens=int(inp),
            completion_tokens=int(out),
            total_tokens=int(inp+out),
        )


async def do_inference(request: Request, body: CreateChatCompletionRequest, ws: "QueueSocket"):
    msize = get_model_size(body.model)
    await ws.queue.put({
        "openai_url": "/v1/chat/completions",
        "openai_req": body.model_dump(mode="json")
    })
    start_time = time.monotonic()
    if body.stream:
        async def stream() -> Iterator[CompletionChunk]:
            prev_js = ""
            while True:
                try:
                    js = await asyncio.wait_for(ws.results.get(), timeout=body.timeout)

                    log.debug("got msg %s", js)

                    if not js and prev_js:
                        # bill when stream is done, for now, could actually charge per stream, but whatever
                        end_time = time.monotonic()
                        check_bill_usage(request, msize, prev_js, ws.info, end_time-start_time)
                        break

                    augment_reply(body, js)

                    prev_js = js

                    yield json.dumps(js)

                    if js.get("error"):
                        log.info("got an error: %s", js["error"])
                        raise HTTPException(status_code=400, detail=json.dumps(js))
                except Exception as ex:
                    log.exception("error during stream")
                    yield json.dumps({"error": str(ex), "error_type": type(ex).__name__})

        return EventSourceResponse(stream())
    else:
        js = await asyncio.wait_for(ws.results.get(), timeout=body.timeout)
        if js.get("error"):
            log.info("got an error: %s", js["error"])
            raise HTTPException(status_code=400, detail=json.dumps(js))
        if ws.info.get("ln_url"):
            js["ln_url"] = ws.info["ln_url"]
        end_time = time.monotonic()
        augment_reply(body, js)
        check_bill_usage(request, msize, js, ws.info, end_time-start_time)
        return js


def get_model_size(model_mame):
    mod = model_mame
    m = re.search(r"(\d)+b(.*)", mod.lower())
    # todo: actually have a nice mapping of model sizes
    if m:
        msize = int(m[1])
        mod = m[2]
    else:
        msize = 13
    m = re.search(r"[Qq](\d)+[_f.-]", mod.lower())
    if m:
        quant = int(m[1])
        if quant == 2:
            msize *= 0.4
        elif quant == 3:
            msize *= 0.5
        elif quant == 4:
            msize *= 0.6
        elif quant == 5:
            msize *= 0.7
        elif quant == 6:
            msize *= 0.8
        elif quant == 8:
            msize *= 1.0
        else:
            # f16
            msize *= 2
    return msize


class QueueSocket(WebSocket):
    queue: Queue
    results: Queue
    info: dict


class Worker:
    def __init__(self, sock, info):
        self.sock = sock
        self.info = info


class WorkerManager:
    def __init__(self):
        self.lock = RLock()
        self.socks = dict[WebSocket, dict]()
        self.busy = dict()

    def register_js(self, sock: WebSocket, info: dict):
        self.socks[sock] = info

    def drop_worker(self, sock):
        self.socks.pop(sock, None)
        self.busy.pop(sock, None)

    @contextlib.contextmanager
    def get_socket_for_inference(self, msize: int, web_only = False, gpu_filter={}) -> Generator[QueueSocket, None, None]:
        # msize is params adjusted by quant level with a heuristic

        # nv gpu memory is reported in MB
        gpu_needed = msize * 1000

        disk_needed = msize * 1000 * 1.5

        # cpu memory is reported in bytes, it's ok to have less... cuz llama.cpp is good about that
        cpu_needed = msize * 1000000000 * 0.75
        
        if web_only:
            cpu_needed = min(cpu_needed, 8000000000)

        with self.lock:
            good = []
            close = []
            for sock, info in self.socks.items():
                cpu_vram = info.get("vram", 0)
                disk_space = info.get("disk_space", 0)
                nv_gpu_ram = sum([el["memory"] for el in info.get("nv_gpus", [])])
                cl_gpu_ram = sum([el["memory"] for el in info.get("cl_gpus", [])])
                have_web_gpus = sum([1 for el in info.get("web_gpus", [])])
                if wid := gpu_filter.get("worker_id"):
                    # used for the autopay cron
                    if info.get("auth_key") != "uid:" + str(wid):
                        continue
                if web_only:
                    if cpu_needed < cpu_vram and have_web_gpus:
                        # very little ability to check here
                        # todo: end the whole self reporting thing and just bench
                        good.append(sock)
                else:
                    if gpu_needed < nv_gpu_ram and cpu_needed < cpu_vram and disk_needed < disk_space:
                        good.append(sock)
                    elif gpu_needed < cl_gpu_ram and cpu_needed < cpu_vram and disk_needed < disk_space:
                        good.append(sock)
                    elif gpu_needed < nv_gpu_ram * 1.2 and cpu_needed < cpu_vram and disk_needed < disk_space:
                        close.append(sock)
                    elif gpu_needed < cl_gpu_ram * 1.2 and cpu_needed < cpu_vram and disk_needed < disk_space:
                        close.append(sock)

            if not good and not close:
                assert False, "No workers available"

            if len(good):
                num = random.randint(0, len(good) - 1)
                choice = good[num]
            elif len(close):
                num = random.randint(0, len(close) - 1)
                choice = close[num]

            info = self.socks.pop(choice)
            self.busy[choice] = info

        choice.info = info

        yield choice

        with self.lock:
            self.socks[choice] = info
            self.busy.pop(choice)

    def set_busy(self, sock, val):
        if val:
            info = self.socks.pop(sock, None)
            if info:
                self.busy[sock] = info
        else:
            info = self.busy.pop(sock, None)
            if info:
                self.socks[sock] = info


g_reg_mgr: Optional[WorkerManager] = None


def get_reg_mgr() -> WorkerManager:
    global g_reg_mgr
    if not g_reg_mgr:
        g_reg_mgr = WorkerManager()
    return g_reg_mgr


@app.websocket("/worker")
async def worker_connect(websocket: WebSocket):
    # request dependencies don't work with websocket, so just roll our own
    await websocket.accept()
    js = await websocket.receive_json()
    mgr = get_reg_mgr()
    log.debug("connect: %s", js)
    mgr.register_js(sock=websocket, info=js)
    websocket.queue = Queue()
    websocket.results = Queue()
    while True:
        try:
            pending = [asyncio.create_task(ent) for ent in [websocket.queue.get(), websocket.receive_json()]]
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                try:
                    action = fut.result()
                    log.info("action %s", action)
                    if "openai_req" in action:
                        while not websocket.results.empty():
                            websocket.results.get_nowait()
                        await websocket.send_json(action)
                    elif "busy" in action:
                        mgr.set_busy(websocket, action.get("busy"))
                    else:
                        log.info("put results")
                        await websocket.results.put(action)
                except (websockets.ConnectionClosedOK, RuntimeError, starlette.websockets.WebSocketDisconnect):
                    raise
                except:
                    log.exception("exception in stuff")
            for ent in pending:
                ent.cancel()
        except (websockets.ConnectionClosedOK, RuntimeError, starlette.websockets.WebSocketDisconnect):
            log.info("dropped worker during send")
            break
    mgr.drop_worker(websocket)
