"""AstSight 下游 Mock 示例服务：报价中心。

唯一用途是让“上游接口 → 下游 HTTP 调用 → Mock 分支注入”这条链路可真实跑通：
- 上游接口 `POST /api/v1/quotes` 调用价格服务 `GET /v1/pricing`；
- 价格服务地址来自环境变量 `PRICING_BASE_URL`，Agent 流程会把它覆盖为
  内嵌 Mock 的 `/_dep/<route_key>` 地址；
- 不接数据库，`/health` 用于就绪探测。

静态契约见同目录 `openapi.json`（分析阶段 SUT 还没启动，不能从 base-url 拉取）。
"""
from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

PRICING_BASE_URL = os.environ.get("PRICING_BASE_URL", "http://127.0.0.1:9998")

app = FastAPI(
    title="AstSight Quote Service",
    version="1.0.0",
    description="报价示例：上游报价接口依赖下游价格服务。",
)


class QuoteRequest(BaseModel):
    sku: str = Field(..., min_length=3, max_length=40)
    quantity: int = Field(..., ge=1, le=99)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/quotes")
async def create_quote(payload: QuoteRequest) -> dict[str, int | str]:
    async with httpx.AsyncClient(timeout=3.0) as client:
        response = await client.get(
            f"{PRICING_BASE_URL}/v1/pricing",
            params={"sku": payload.sku},
        )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="sku not found")
    response.raise_for_status()
    unit_price_cents = int(response.json()["unit_price_cents"])
    return {
        "sku": payload.sku,
        "quantity": payload.quantity,
        "total_cents": unit_price_cents * payload.quantity,
    }
