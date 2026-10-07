"""The script process: runs one page's JavaScript, isolated from the browser.

The browser starts this file as a separate OS process for each page that has
scripts (see js.py). It runs QuickJS with js_prelude.js and nothing else; it
does not import the browser, so page code has nothing in this process to
reach except QuickJS and the single `__native` function, which only sends a
request back to the browser over stdin/stdout and returns the answer.

Because it is its own process, the browser can enforce a hard time limit by
killing it, and a crash in the interpreter cannot take the window down.

Messages are 4-byte little-endian length + JSON:
  browser -> worker: ["eval", code] | ["call", name, [args]] | ["ret", json] | ["exit"]
  worker -> browser: ["ready"] | ["native", op, [args]] | ["done", result, error, overflow]
"""
import json
import os
import struct
import sys
import threading

MAX_JOBS = 100_000


def main():
    memory_limit = int(sys.argv[1]) if len(sys.argv) > 1 else 64 * 1024 * 1024
    stack_limit = int(sys.argv[2]) if len(sys.argv) > 2 else 4 * 1024 * 1024
    inp = sys.stdin.buffer
    out = sys.stdout.buffer
    sys.stdout = sys.stderr          # nothing else may write to the message pipe

    def read_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = inp.read(n - len(buf))
            if not chunk:
                os._exit(0)          # the browser went away
            buf += chunk
        return buf

    def recv():
        n = struct.unpack("<I", read_exact(4))[0]
        return json.loads(read_exact(n).decode("utf-8"))

    def send(obj):
        data = json.dumps(obj).encode("utf-8")
        out.write(struct.pack("<I", len(data)) + data)
        out.flush()

    import quickjs

    def native(op, *args):
        # Only primitives cross; JS objects arrive as quickjs.Object and are dropped.
        clean = [a if a is None or isinstance(a, (str, int, float, bool)) else None for a in args]
        try:
            send(["native", op if isinstance(op, str) else "", clean])
            msg = recv()
            return msg[1]            # a JSON string {"v": value} or {"e": "message"}
        except Exception:            # never let a Python exception into QuickJS
            return '{"e": "InternalError: browser connection lost"}'

    ctx = quickjs.Context()
    ctx.set_memory_limit(memory_limit)
    ctx.set_max_stack_size(stack_limit)
    ctx.add_callable("__native", native)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "js_prelude.js"), encoding="utf-8") as f:
        ctx.eval(f.read())
    fns = {}
    send(["ready"])
    while True:
        msg = recv()
        kind = msg[0]
        if kind == "exit":
            break
        result, error = None, None
        try:
            if kind == "eval":
                result = ctx.eval(msg[1])
            else:
                name = msg[1]
                if name not in fns:
                    fns[name] = ctx.get("__host_" + name)
                result = fns[name](*msg[2])
        except Exception as e:
            error = str(e)
        if not (result is None or isinstance(result, (str, int, float, bool))):
            result = None
        jobs, overflow = 0, False
        while True:
            try:
                if not ctx.execute_pending_job():
                    break
            except Exception as e:
                error = error or str(e)
            jobs += 1
            if jobs >= MAX_JOBS:
                overflow = True
                break
        send(["done", result, error, overflow])


if __name__ == "__main__":
    # Run on a thread with a big stack, so deep (limited) JS recursion fails
    # with a clean RangeError instead of crashing the process.
    threading.stack_size(64 * 1024 * 1024)
    t = threading.Thread(target=main)
    t.start()
    t.join()
