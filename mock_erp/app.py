"""Mock of an S/4HANA-style purchase order OData endpoint. Deliberately unreliable.

This stands in for the customer's SAP system, so it does not import anything from procure:
it only knows the wire format.

  POST {PO_PATH}   create a PO. Requires an Idempotency-Key header.
  GET  {PO_PATH}   list POs (OData v2 shape: {"d": {"results": [...]}})

Faults (probability FLAKY_RATE, default 0.3; FLAKY_SEED makes them repeatable):
  503_before  service unavailable, nothing created
  429         rate limited, Retry-After: 1, nothing created
  503_after   the PO IS created, then the response is lost (503). Without idempotency, the
              client's retry would create a second PO. This is the case the key exists for.

Idempotency: same key + same body -> the existing PO is returned (header Idempotent-Replayed).
Same key + different body -> 409, because it means a bug on the caller's side.

Run: make erp  (uvicorn on :8001). Storage is in memory; restarting clears it.
"""

import hashlib
import json
import os
import random
import threading
from collections import Counter
from decimal import Decimal
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

PO_PATH = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"
FAULTS = ["503_before", "429", "503_after"]


class PurchaseOrderItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    Material: str
    OrderQuantity: Decimal = Field(gt=0)
    NetPriceAmount: Decimal = Field(gt=0)


class PurchaseOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    PurchaseOrderType: str
    CompanyCode: str
    Supplier: str
    DocumentCurrency: str
    to_PurchaseOrderItem: list[PurchaseOrderItemIn] = Field(min_length=1)


class Store:
    """In-memory POs. One lock, so concurrent retries cannot both create a PO for one key."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pos: list[dict[str, Any]] = []
        self.by_key: dict[str, tuple[str, dict[str, Any]]] = {}  # key -> (body hash, PO)
        self.next_number = 4500000001  # SAP's usual number range for standard POs
        self.faults: Counter[str] = Counter()  # how often each fault fired (for tests)


def create_app(
    flaky_rate: float | None = None,
    seed: int | None = None,
    script: list[str | None] | None = None,
) -> FastAPI:
    """`script` overrides randomness: one entry per POST (a fault name or None), then no faults."""
    rate = float(os.environ.get("FLAKY_RATE", "0.3")) if flaky_rate is None else flaky_rate
    env_seed = os.environ.get("FLAKY_SEED")
    rng = random.Random(seed if seed is not None else (int(env_seed) if env_seed else None))
    planned = list(script) if script is not None else None

    app = FastAPI(title="Mock SAP purchase order API")
    store = Store()
    app.state.store = store

    def next_fault() -> str | None:
        if planned is not None:
            return planned.pop(0) if planned else None
        return rng.choice(FAULTS) if rng.random() < rate else None

    @app.post(PO_PATH)
    def create_purchase_order(
        po: PurchaseOrderIn,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        fault = next_fault()
        if fault:
            store.faults[fault] += 1
        if fault == "503_before":
            return JSONResponse({"error": "service unavailable"}, status_code=503)
        if fault == "429":
            return JSONResponse(
                {"error": "rate limited"}, status_code=429, headers={"Retry-After": "1"}
            )

        body = po.model_dump(mode="json")
        body_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with store.lock:
            existing = store.by_key.get(idempotency_key)
            if existing is not None:
                if existing[0] != body_hash:
                    raise HTTPException(409, "Idempotency-Key reused with a different body")
                record, replayed = existing[1], True
            else:
                record = {
                    "PurchaseOrder": str(store.next_number),
                    **{k: v for k, v in body.items() if k != "to_PurchaseOrderItem"},
                    "to_PurchaseOrderItem": {"results": body["to_PurchaseOrderItem"]},
                    "IdempotencyKey": idempotency_key,  # mock-only field, for inspection
                }
                store.next_number += 1
                store.pos.append(record)
                store.by_key[idempotency_key] = (body_hash, record)
                replayed = False

        if fault == "503_after":  # created, but the caller never hears about it
            return JSONResponse({"error": "service unavailable"}, status_code=503)
        headers = {"Idempotent-Replayed": "true"} if replayed else {}
        return JSONResponse({"d": record}, status_code=201, headers=headers)

    @app.get(PO_PATH)
    def list_purchase_orders() -> dict[str, Any]:
        with store.lock:
            return {"d": {"results": list(store.pos)}}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
