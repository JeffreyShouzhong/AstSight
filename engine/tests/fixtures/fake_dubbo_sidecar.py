#!/usr/bin/env python3
"""按 sidecar JSONL 协议回话的假进程，只用于客户端单测。"""
from __future__ import annotations

import json
import sys
import time


def send(value: dict) -> None:
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        request_id = request.get("id")
        op = request.get("op")
        if op == "ping":
            send(
                {
                    "id": request_id,
                    "ok": True,
                    "protocol": 1,
                    "sidecar_version": "test",
                    "dubbo_version": "2.7.22",
                    "java_version": "test",
                }
            )
        elif op == "shutdown":
            send({"id": request_id, "ok": True})
            return 0
        elif op == "invoke":
            method = request.get("method")
            if method == "fail":
                send(
                    {
                        "id": request_id,
                        "ok": False,
                        "error": "rpc_exception",
                        "rpc_status": "RPC_EXCEPTION",
                        "exception_type": "java.lang.IllegalStateException",
                        "message": "boom",
                        "elapsed_ms": 3,
                    }
                )
            elif method == "hang":
                time.sleep(1.5)
            elif method == "noisy":
                sys.stdout.write("not-json\n")
                sys.stdout.flush()
                send(
                    {
                        "id": request_id,
                        "ok": True,
                        "value": None,
                        "rpc_status": "OK",
                        "elapsed_ms": 1,
                    }
                )
            elif method == "exit":
                return 3
            else:
                send(
                    {
                        "id": request_id,
                        "ok": True,
                        "value": {
                            "interface": request.get("interface"),
                            "method": method,
                            "arguments": request.get("arguments"),
                            "attachments": request.get("attachments"),
                        },
                        "rpc_status": "OK",
                        "elapsed_ms": 1,
                    }
                )
        else:
            send(
                {
                    "id": request_id,
                    "ok": False,
                    "error": "bad_request",
                    "message": "unknown op",
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
